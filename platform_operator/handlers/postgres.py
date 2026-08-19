"""
platform_operator/handlers/postgres.py

Kopf handler for the PostgresInstance CRD and the Postgres sub-component
of PlatformStack.

Deploys PostgreSQL via Helm using the online OCI bitnamicharts/postgresql chart.
Istio sidecar handles mTLS; plain TCP 5432 internally.
Credentials managed via K8s Secret.
"""

import base64
import logging
import secrets
import string

import kopf
from kubernetes import client

from platform_operator.config.defaults import CHARTS, CHART_VERSIONS, POSTGRES_DEFAULTS
from platform_operator.helm.helm_client import (
    HelmError,
    helm_is_deployed,
    helm_install,
    helm_uninstall,
    helm_upgrade,
)
from platform_operator.helm.health_check import (
    _ensure_k8s,
    ensure_namespace,
    get_pod_health,
    wait_for_statefulset_ready,
)

logger = logging.getLogger(__name__)

GROUP   = "platform.ops"
VERSION = "v1alpha1"

POSTGRES_NS        = "postgres"
CREDENTIALS_SECRET = "postgres-credentials"
POSTGRES_RELEASE   = "postgresql"


# ─────────────────────────────────────────────────────────────────────────────
# Standalone PostgresInstance CRD handlers
# ─────────────────────────────────────────────────────────────────────────────

@kopf.on.create(GROUP, VERSION, "postgresinstances")
def postgres_create(spec, name, namespace, patch, **kwargs):
    logger.info("PostgresInstance '%s' created — deploying PostgreSQL...", name)
    patch.status["phase"] = "Installing"
    _deploy_postgres(spec, patch)


@kopf.on.update(GROUP, VERSION, "postgresinstances")
def postgres_update(spec, name, old, new, patch, **kwargs):
    logger.info("PostgresInstance '%s' updated — upgrading PostgreSQL...", name)
    patch.status["phase"] = "Upgrading"
    _upgrade_postgres(spec, patch)


@kopf.on.delete(GROUP, VERSION, "postgresinstances")
def postgres_delete(spec, name, patch, **kwargs):
    logger.info("PostgresInstance '%s' deleted — tearing down PostgreSQL...", name)
    _teardown_postgres(spec)


@kopf.on.timer(GROUP, VERSION, "postgresinstances", interval=60.0, idle=30.0)
def postgres_health_timer(spec, name, patch, **kwargs):
    health = get_pod_health("app.kubernetes.io/name=postgresql", POSTGRES_NS)
    patch.status["health"] = health
    patch.status["ready"] = health.get("running", 0) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Shared install logic (called by PlatformStack handler)
# ─────────────────────────────────────────────────────────────────────────────

def deploy_postgres_for_platform(spec: dict, patch) -> None:
    _deploy_postgres(spec, patch)


def upgrade_postgres_for_platform(spec: dict, patch) -> None:
    _upgrade_postgres(spec, patch)


def teardown_postgres(spec: dict) -> None:
    _teardown_postgres(spec)


def get_postgres_connection_secret() -> str:
    """Return the name of the Secret containing postgres credentials."""
    return CREDENTIALS_SECRET


# ─────────────────────────────────────────────────────────────────────────────
# Internal implementation
# ─────────────────────────────────────────────────────────────────────────────

def _deploy_postgres(spec: dict, patch) -> None:
    ensure_namespace(POSTGRES_NS)

    db_name  = spec.get("database", "appdb")
    username = spec.get("username", "appuser")
    storage  = spec.get("storageSize", "10Gi")
    replicas = spec.get("replicas", 1)

    # ── Step 1: Generate & store credentials as a K8s Secret ─────────────────
    patch.status["phase"] = "Creating credentials secret"
    password = _ensure_credentials_secret(username, db_name)

    try:
        # ── Step 2: Helm install/upgrade postgresql ───────────────────────────
        patch.status["phase"] = "Deploying postgresql"
        helm_upgrade(
            release=POSTGRES_RELEASE,
            chart=CHARTS["postgresql"],
            namespace=POSTGRES_NS,
            values_file="postgres-values.yaml",
            version=CHART_VERSIONS["postgresql"],
            set_values={
                **POSTGRES_DEFAULTS,
                "auth.username": username,
                "auth.database": db_name,
                "auth.password": password,
                "auth.postgresPassword": password,
                "primary.persistence.size": storage,
                "primary.replicaCount": str(replicas),
            },
            install=True,
            timeout="10m",
        )

        # ── Step 3: Wait for StatefulSet to be ready ──────────────────────────
        patch.status["phase"] = "Waiting for PostgreSQL to be ready"
        wait_for_statefulset_ready("postgresql", POSTGRES_NS, timeout=300)

        patch.status["phase"]             = "Ready"
        patch.status["ready"]             = True
        patch.status["database"]          = db_name
        patch.status["username"]          = username
        patch.status["namespace"]         = POSTGRES_NS
        patch.status["credentialsSecret"] = CREDENTIALS_SECRET
        logger.info("PostgreSQL deployed and ready ✓ (ns=%s, db=%s)", POSTGRES_NS, db_name)

    except HelmError as exc:
        patch.status["phase"] = "Error"
        patch.status["error"] = str(exc)
        raise kopf.TemporaryError(f"Helm error deploying PostgreSQL: {exc}", delay=60)
    except TimeoutError as exc:
        patch.status["phase"] = "Error"
        patch.status["error"] = str(exc)
        raise kopf.TemporaryError(str(exc), delay=90)


def _upgrade_postgres(spec: dict, patch) -> None:
    storage  = spec.get("storageSize", "10Gi")
    replicas = spec.get("replicas", 1)

    try:
        helm_upgrade(
            release=POSTGRES_RELEASE,
            chart=CHARTS["postgresql"],
            namespace=POSTGRES_NS,
            values_file="postgres-values.yaml",
            version=CHART_VERSIONS["postgresql"],
            set_values={
                **POSTGRES_DEFAULTS,
                "primary.persistence.size": storage,
                "primary.replicaCount": str(replicas),
            },
        )
        wait_for_statefulset_ready("postgresql", POSTGRES_NS, timeout=300)
        patch.status["phase"] = "Ready"
        logger.info("PostgreSQL upgraded ✓")
    except (HelmError, TimeoutError) as exc:
        patch.status["phase"] = "Error"
        raise kopf.TemporaryError(str(exc), delay=60)


def _teardown_postgres(spec: dict) -> None:
    helm_uninstall(POSTGRES_RELEASE, POSTGRES_NS, ignore_not_found=True)
    _delete_secret(CREDENTIALS_SECRET, POSTGRES_NS)
    logger.info("PostgreSQL teardown complete ✓")


# ─────────────────────────────────────────────────────────────────────────────
# Credential helpers
# ─────────────────────────────────────────────────────────────────────────────

def _generate_password(length: int = 24) -> str:
    """Generate a secure alphanumeric password."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _ensure_credentials_secret(username: str, db_name: str) -> str:
    """
    Create the postgres-credentials Secret if it doesn't exist.
    Returns the plaintext password (used for helm --set).
    """
    _ensure_k8s()
    core_v1 = client.CoreV1Api()

    try:
        secret = core_v1.read_namespaced_secret(CREDENTIALS_SECRET, POSTGRES_NS)
        password_b64 = secret.data.get("password", "")
        return base64.b64decode(password_b64).decode()
    except client.ApiException as exc:
        if exc.status != 404:
            raise

    password = _generate_password()
    secret_body = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=CREDENTIALS_SECRET,
            namespace=POSTGRES_NS,
            labels={"managed-by": "kopf-operator"},
        ),
        type="Opaque",
        data={
            "username":          _b64(username),
            "password":          _b64(password),
            "database":          _b64(db_name),
            "host":              _b64(f"postgresql.{POSTGRES_NS}.svc.cluster.local"),
            "port":              _b64("5432"),
            "connection-string": _b64(
                f"postgresql://{username}:{password}@postgresql.{POSTGRES_NS}.svc.cluster.local:5432/{db_name}"
            ),
        },
    )
    core_v1.create_namespaced_secret(POSTGRES_NS, secret_body)
    logger.info("Created credentials Secret '%s' in ns=%s", CREDENTIALS_SECRET, POSTGRES_NS)
    return password


def _delete_secret(name: str, namespace: str) -> None:
    _ensure_k8s()
    core_v1 = client.CoreV1Api()
    try:
        core_v1.delete_namespaced_secret(name, namespace)
        logger.info("Deleted Secret '%s' from ns=%s", name, namespace)
    except client.ApiException as exc:
        if exc.status != 404:
            logger.warning("Could not delete Secret '%s': %s", name, exc)

