"""
platform_operator/handlers/airflow.py

Kopf handler for the AirflowInstance CRD and the Airflow sub-component
of PlatformStack.

Key design decisions:
  - Uses KubernetesExecutor (no Redis/Celery — conserves RAM on 8GB VM)
  - Connects to the external PostgreSQL deployed by the postgres handler
    using the 'postgres-credentials' Secret
  - No per-app TLS — Istio sidecar + Gateway handles HTTPS
  - Airflow webserver accessible via /airflow VirtualService
  - git-sync DAGs: disabled by default, configurable via CRD spec
  - Depends on PostgreSQL being Ready before install (checked via Secret)
"""

import base64
import logging

import kopf
from kubernetes import client

from platform_operator.config.defaults import AIRFLOW_DEFAULTS, CHARTS, CHART_VERSIONS
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
    wait_for_deployment_ready,
)
from platform_operator.handlers.postgres import CREDENTIALS_SECRET, POSTGRES_NS
from platform_operator.tls.istio_tls import apply_virtual_service

logger = logging.getLogger(__name__)

GROUP   = "platform.ops"
VERSION = "v1alpha1"

AIRFLOW_NS      = "airflow"
AIRFLOW_RELEASE = "airflow"


# ─────────────────────────────────────────────────────────────────────────────
# Standalone AirflowInstance CRD handlers
# ─────────────────────────────────────────────────────────────────────────────

@kopf.on.create(GROUP, VERSION, "airflowinstances")
def airflow_create(spec, name, namespace, patch, **kwargs):
    logger.info("AirflowInstance '%s' created — deploying Airflow...", name)
    patch.status["phase"] = "Installing"
    _deploy_airflow(spec, patch)


@kopf.on.update(GROUP, VERSION, "airflowinstances")
def airflow_update(spec, name, old, new, patch, **kwargs):
    logger.info("AirflowInstance '%s' updated — upgrading Airflow...", name)
    patch.status["phase"] = "Upgrading"
    _upgrade_airflow(spec, patch)


@kopf.on.delete(GROUP, VERSION, "airflowinstances")
def airflow_delete(spec, name, patch, **kwargs):
    logger.info("AirflowInstance '%s' deleted — tearing down Airflow...", name)
    _teardown_airflow()


@kopf.on.timer(GROUP, VERSION, "airflowinstances", interval=60.0, idle=30.0)
def airflow_health_timer(spec, name, patch, **kwargs):
    health = _check_airflow_health()
    patch.status["health"] = health
    patch.status["ready"] = health.get("webserver", {}).get("running", 0) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Shared logic (called by PlatformStack)
# ─────────────────────────────────────────────────────────────────────────────

def deploy_airflow_for_platform(spec: dict, patch) -> None:
    _deploy_airflow(spec, patch)


def upgrade_airflow_for_platform(spec: dict, patch) -> None:
    _upgrade_airflow(spec, patch)


def teardown_airflow() -> None:
    _teardown_airflow()


# ─────────────────────────────────────────────────────────────────────────────
# Internal implementation
# ─────────────────────────────────────────────────────────────────────────────

def _deploy_airflow(spec: dict, patch) -> None:
    ensure_namespace(AIRFLOW_NS)

    executor       = spec.get("executor", "KubernetesExecutor")
    git_sync       = spec.get("gitSync", {})
    git_enabled    = git_sync.get("enabled", False)
    git_repo       = git_sync.get("repo", "")
    git_branch     = git_sync.get("branch", "main")
    git_subpath    = git_sync.get("subPath", "dags")
    webserver_reps = spec.get("webserverReplicas", 1)
    scheduler_reps = spec.get("schedulerReplicas", 1)

    # ── Step 1: Verify PostgreSQL credentials Secret exists ───────────────────
    patch.status["phase"] = "Checking PostgreSQL dependency"
    pg_creds = _read_postgres_credentials()
    if not pg_creds:
        raise kopf.TemporaryError(
            "PostgreSQL credentials secret not found — ensure PostgreSQL is deployed first.",
            delay=30,
        )

    try:
        # ── Step 2: Build Helm set-values ─────────────────────────────────────
        set_values = {
            **AIRFLOW_DEFAULTS,
            "executor":                    executor,
            "webserver.replicas":          str(webserver_reps),
            "scheduler.replicas":          str(scheduler_reps),
            # External PostgreSQL connection
            "data.metadataConnection.host":     pg_creds["host"],
            "data.metadataConnection.port":     pg_creds["port"],
            "data.metadataConnection.user":     pg_creds["username"],
            "data.metadataConnection.pass":     pg_creds["password"],
            "data.metadataConnection.db":       pg_creds["database"],
            "data.metadataConnection.protocol": "postgresql",
            # Internal PostgreSQL disabled (we use external)
            "postgresql.enabled": "false",
            "redis.enabled":      "false",
            # Webserver runs plain HTTP — Istio Gateway serves /airflow over HTTPS
            "webserver.service.type": "ClusterIP",
            "config.webserver.base_url":    "https://192.168.56.50/airflow",
            "config.webserver.enable_proxy_fix": "True",
            # git-sync
            "dags.gitSync.enabled":  str(git_enabled).lower(),
        }

        if git_enabled and git_repo:
            set_values.update({
                "dags.gitSync.repo":    git_repo,
                "dags.gitSync.branch":  git_branch,
                "dags.gitSync.subPath": git_subpath,
                "dags.gitSync.depth":   "1",
                "dags.gitSync.wait":    "60",
            })

        # ── Step 3: Helm install/upgrade Airflow ──────────────────────────────
        patch.status["phase"] = "Deploying Airflow"
        helm_upgrade(
            release=AIRFLOW_RELEASE,
            chart=CHARTS["airflow"],
            namespace=AIRFLOW_NS,
            values_file="airflow-values.yaml",
            version=CHART_VERSIONS["airflow"],
            set_values=set_values,
            install=True,
            timeout="15m",
        )

        # ── Step 4: Wait for webserver and scheduler ──────────────────────────
        patch.status["phase"] = "Waiting for Airflow webserver"
        wait_for_deployment_ready("airflow-webserver", AIRFLOW_NS, timeout=360)

        patch.status["phase"] = "Waiting for Airflow scheduler"
        wait_for_deployment_ready("airflow-scheduler", AIRFLOW_NS, timeout=300)

        # ── Step 5: Expose via Istio Ingress Gateway VirtualService ──────────
        apply_virtual_service(
            name="airflow-vs",
            service_name="airflow-webserver",
            service_namespace=AIRFLOW_NS,
            service_port=8080,
            uri_prefix="/airflow",
        )

        patch.status["phase"]       = "Ready"
        patch.status["ready"]       = True
        patch.status["executor"]    = executor
        patch.status["webserverUrl"]= "https://192.168.56.50/airflow"
        patch.status["gitSync"]     = git_enabled
        logger.info("Airflow deployed and ready ✓ (executor=%s)", executor)

    except HelmError as exc:
        patch.status["phase"] = "Error"
        patch.status["error"] = str(exc)
        raise kopf.TemporaryError(f"Helm error deploying Airflow: {exc}", delay=60)
    except TimeoutError as exc:
        patch.status["phase"] = "Error"
        patch.status["error"] = str(exc)
        raise kopf.TemporaryError(str(exc), delay=90)


def _upgrade_airflow(spec: dict, patch) -> None:
    executor    = spec.get("executor", "KubernetesExecutor")
    pg_creds    = _read_postgres_credentials()

    try:
        set_values = {
            **AIRFLOW_DEFAULTS,
            "executor": executor,
            "postgresql.enabled": "false",
            "redis.enabled": "false",
        }
        if pg_creds:
            set_values.update({
                "data.metadataConnection.host": pg_creds["host"],
                "data.metadataConnection.user": pg_creds["username"],
                "data.metadataConnection.pass": pg_creds["password"],
                "data.metadataConnection.db":   pg_creds["database"],
            })

        helm_upgrade(
            release=AIRFLOW_RELEASE,
            chart=CHARTS["airflow"],
            namespace=AIRFLOW_NS,
            values_file="airflow-values.yaml",
            version=CHART_VERSIONS["airflow"],
            set_values=set_values,
        )
        wait_for_deployment_ready("airflow-webserver", AIRFLOW_NS, timeout=360)
        patch.status["phase"] = "Ready"
        logger.info("Airflow upgraded ✓")
    except (HelmError, TimeoutError) as exc:
        patch.status["phase"] = "Error"
        raise kopf.TemporaryError(str(exc), delay=60)


def _teardown_airflow() -> None:
    helm_uninstall(AIRFLOW_RELEASE, AIRFLOW_NS, ignore_not_found=True)
    logger.info("Airflow teardown complete ✓")


def _check_airflow_health() -> dict:
    return {
        "webserver": get_pod_health("component=webserver", AIRFLOW_NS),
        "scheduler": get_pod_health("component=scheduler", AIRFLOW_NS),
        "workers":   get_pod_health("component=worker", AIRFLOW_NS),
    }


def _read_postgres_credentials() -> dict | None:
    """Read the postgres-credentials Secret and return decoded values."""
    _ensure_k8s()
    core_v1 = client.CoreV1Api()
    try:
        secret = core_v1.read_namespaced_secret(CREDENTIALS_SECRET, POSTGRES_NS)
        return {
            key: base64.b64decode(val).decode()
            for key, val in secret.data.items()
        }
    except Exception:
        return None
