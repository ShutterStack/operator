"""
platform_operator/handlers/monitoring.py

Kopf handler for the MonitoringStack CRD and the monitoring sub-component
of PlatformStack.

Deploys kube-prometheus-stack (Prometheus Operator + Grafana bundled together).
Istio sidecar injection is pre-applied by the Istio handler.
Grafana is exposed via the Istio Gateway VirtualService (no per-app TLS).
Grafana admin credentials are stored in a Kubernetes Secret.
"""

import base64
import logging
import secrets
import string

import kopf
from kubernetes import client

from platform_operator.config.defaults import CHARTS, CHART_VERSIONS, PROMETHEUS_DEFAULTS
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
from platform_operator.tls.istio_tls import apply_virtual_service

logger = logging.getLogger(__name__)

GROUP   = "platform.ops"
VERSION = "v1alpha1"

MONITORING_NS      = "monitoring"
GRAFANA_SECRET     = "grafana-admin-credentials"
PROMETHEUS_RELEASE = "kube-prometheus-stack"


# ─────────────────────────────────────────────────────────────────────────────
# Standalone MonitoringStack CRD handlers
# ─────────────────────────────────────────────────────────────────────────────

@kopf.on.create(GROUP, VERSION, "monitoringstacks")
def monitoring_create(spec, name, namespace, patch, **kwargs):
    logger.info("MonitoringStack '%s' created — deploying Prometheus + Grafana...", name)
    patch.status["phase"] = "Installing"
    _deploy_monitoring(spec, patch)


@kopf.on.update(GROUP, VERSION, "monitoringstacks")
def monitoring_update(spec, name, old, new, patch, **kwargs):
    logger.info("MonitoringStack '%s' updated — upgrading...", name)
    patch.status["phase"] = "Upgrading"
    _upgrade_monitoring(spec, patch)


@kopf.on.delete(GROUP, VERSION, "monitoringstacks")
def monitoring_delete(spec, name, patch, **kwargs):
    logger.info("MonitoringStack '%s' deleted — tearing down monitoring...", name)
    _teardown_monitoring()


@kopf.on.timer(GROUP, VERSION, "monitoringstacks", interval=60.0, idle=30.0)
def monitoring_health_timer(spec, name, patch, **kwargs):
    health = _check_monitoring_health()
    patch.status["health"] = health
    patch.status["ready"] = (
        health.get("prometheus", {}).get("running", 0) > 0
        and health.get("grafana", {}).get("running", 0) > 0
    )


# ─────────────────────────────────────────────────────────────────────────────
# Shared logic (called by PlatformStack)
# ─────────────────────────────────────────────────────────────────────────────

def deploy_monitoring_for_platform(spec: dict, patch) -> None:
    _deploy_monitoring(spec, patch)


def upgrade_monitoring_for_platform(spec: dict, patch) -> None:
    _upgrade_monitoring(spec, patch)


def teardown_monitoring() -> None:
    _teardown_monitoring()


# ─────────────────────────────────────────────────────────────────────────────
# Internal implementation
# ─────────────────────────────────────────────────────────────────────────────

def _deploy_monitoring(spec: dict, patch) -> None:
    ensure_namespace(MONITORING_NS)

    grafana_enabled  = spec.get("grafana", {}).get("enabled", True)
    storage_size     = spec.get("storageSize", "2Gi")
    retention        = spec.get("retention", "7d")

    # ── Step 1: Create Grafana admin credentials Secret ───────────────────────
    patch.status["phase"] = "Creating Grafana credentials"
    admin_password = _ensure_grafana_secret(
        spec.get("grafana", {}).get("adminPassword", None)
    )

    try:
        # ── Step 2: Helm install/upgrade kube-prometheus-stack ────────────────
        patch.status["phase"] = "Deploying kube-prometheus-stack"
        helm_upgrade(
            release=PROMETHEUS_RELEASE,
            chart=CHARTS["kube_prometheus_stack"],
            namespace=MONITORING_NS,
            values_file="prometheus-values.yaml",
            version=CHART_VERSIONS["kube_prometheus_stack"],
            set_values={
                **PROMETHEUS_DEFAULTS,
                "grafana.enabled":        str(grafana_enabled).lower(),
                "grafana.adminPassword":  admin_password,
                "prometheus.prometheusSpec.retention": retention,
                "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage": storage_size,
                # Grafana runs plain HTTP — Istio Gateway handles HTTPS
                "grafana.service.type":   "ClusterIP",
                "grafana.env.GF_SERVER_ROOT_URL": "%(protocol)s://%(domain)s/grafana",
                "grafana.env.GF_SERVER_SERVE_FROM_SUB_PATH": "true",
            },
            install=True,
            timeout="12m",
        )

        # ── Step 3: Wait for key deployments ─────────────────────────────────
        patch.status["phase"] = "Waiting for Prometheus operator"
        wait_for_deployment_ready(
            f"{PROMETHEUS_RELEASE}-operator", MONITORING_NS, timeout=300
        )

        if grafana_enabled:
            patch.status["phase"] = "Waiting for Grafana"
            wait_for_deployment_ready(
                f"{PROMETHEUS_RELEASE}-grafana", MONITORING_NS, timeout=300
            )

        # ── Step 4: Expose via Istio Ingress Gateway VirtualServices ──────────
        apply_virtual_service(
            name="prometheus-vs",
            service_name=f"{PROMETHEUS_RELEASE}-prometheus",
            service_namespace=MONITORING_NS,
            service_port=9090,
            uri_prefix="/prometheus",
        )
        if grafana_enabled:
            apply_virtual_service(
                name="grafana-vs",
                service_name=f"{PROMETHEUS_RELEASE}-grafana",
                service_namespace=MONITORING_NS,
                service_port=80,
                uri_prefix="/grafana",
            )

        patch.status["phase"]    = "Ready"
        patch.status["ready"]    = True
        patch.status["grafanaCredentialsSecret"] = GRAFANA_SECRET
        patch.status["grafanaUrl"] = "https://192.168.56.50/grafana"
        patch.status["prometheusUrl"] = "https://192.168.56.50/prometheus"
        logger.info("Monitoring stack (Prometheus + Grafana) deployed via Istio Gateway ✓")


    except HelmError as exc:
        patch.status["phase"] = "Error"
        patch.status["error"] = str(exc)
        raise kopf.TemporaryError(f"Helm error deploying monitoring: {exc}", delay=60)
    except TimeoutError as exc:
        patch.status["phase"] = "Error"
        patch.status["error"] = str(exc)
        raise kopf.TemporaryError(str(exc), delay=90)


def _upgrade_monitoring(spec: dict, patch) -> None:
    retention    = spec.get("retention", "7d")
    storage_size = spec.get("storageSize", "10Gi")

    try:
        helm_upgrade(
            release=PROMETHEUS_RELEASE,
            chart=CHARTS["kube_prometheus_stack"],
            namespace=MONITORING_NS,
            values_file="prometheus-values.yaml",
            version=CHART_VERSIONS["kube_prometheus_stack"],
            set_values={
                **PROMETHEUS_DEFAULTS,
                "prometheus.prometheusSpec.retention": retention,
                "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage": storage_size,
            },
        )
        wait_for_deployment_ready(f"{PROMETHEUS_RELEASE}-operator", MONITORING_NS, timeout=300)
        patch.status["phase"] = "Ready"
        logger.info("Monitoring stack upgraded ✓")
    except (HelmError, TimeoutError) as exc:
        patch.status["phase"] = "Error"
        raise kopf.TemporaryError(str(exc), delay=60)


def _teardown_monitoring() -> None:
    helm_uninstall(PROMETHEUS_RELEASE, MONITORING_NS, ignore_not_found=True)
    # Remove CRDs installed by kube-prometheus-stack
    _cleanup_prometheus_crds()
    _delete_secret(GRAFANA_SECRET, MONITORING_NS)
    logger.info("Monitoring stack teardown complete ✓")


def _check_monitoring_health() -> dict:
    return {
        "prometheus": get_pod_health(
            "app.kubernetes.io/name=prometheus", MONITORING_NS
        ),
        "grafana": get_pod_health(
            "app.kubernetes.io/name=grafana", MONITORING_NS
        ),
        "operator": get_pod_health(
            f"app.kubernetes.io/name={PROMETHEUS_RELEASE}-operator", MONITORING_NS
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Secret helpers
# ─────────────────────────────────────────────────────────────────────────────

def _generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _ensure_grafana_secret(admin_password_override: str | None) -> str:
    _ensure_k8s()
    core_v1 = client.CoreV1Api()

    try:
        secret = core_v1.read_namespaced_secret(GRAFANA_SECRET, MONITORING_NS)
        return base64.b64decode(secret.data["admin-password"]).decode()
    except client.ApiException as exc:
        if exc.status != 404:
            raise

    password = admin_password_override or _generate_password()
    secret_body = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=GRAFANA_SECRET,
            namespace=MONITORING_NS,
            labels={"managed-by": "kopf-operator"},
        ),
        type="Opaque",
        data={
            "admin-user":     _b64("admin"),
            "admin-password": _b64(password),
        },
    )
    core_v1.create_namespaced_secret(MONITORING_NS, secret_body)
    logger.info("Created Grafana credentials Secret in ns=%s", MONITORING_NS)
    return password


def _delete_secret(name: str, namespace: str) -> None:
    _ensure_k8s()
    core_v1 = client.CoreV1Api()
    try:
        core_v1.delete_namespaced_secret(name, namespace)
    except client.ApiException as exc:
        if exc.status != 404:
            logger.warning("Could not delete Secret '%s': %s", name, exc)


def _cleanup_prometheus_crds() -> None:
    """Remove Prometheus Operator CRDs that Helm leaves behind."""
    from kubernetes import client as k8s_client
    _ensure_k8s()
    ext = k8s_client.ApiextensionsV1Api()
    crds_to_remove = [
        "alertmanagerconfigs.monitoring.coreos.com",
        "alertmanagers.monitoring.coreos.com",
        "podmonitors.monitoring.coreos.com",
        "probes.monitoring.coreos.com",
        "prometheuses.monitoring.coreos.com",
        "prometheusrules.monitoring.coreos.com",
        "servicemonitors.monitoring.coreos.com",
        "thanosrulers.monitoring.coreos.com",
    ]
    for crd_name in crds_to_remove:
        try:
            ext.delete_custom_resource_definition(crd_name)
            logger.debug("Deleted CRD: %s", crd_name)
        except client.ApiException as exc:
            if exc.status != 404:
                logger.warning("Could not delete CRD %s: %s", crd_name, exc)


def _ensure_prometheus_nodeport(node_port: int = 30090) -> None:
    """Create or verify the NodePort service exposing Prometheus on port 30090."""
    _ensure_k8s()
    core_v1 = client.CoreV1Api()
    svc_name = "prometheus-nodeport"

    try:
        core_v1.read_namespaced_service(svc_name, MONITORING_NS)
        logger.debug("Service '%s' already exists in ns=%s", svc_name, MONITORING_NS)
        return
    except client.ApiException as exc:
        if exc.status != 404:
            raise

    svc_body = client.V1Service(
        metadata=client.V1ObjectMeta(
            name=svc_name,
            namespace=MONITORING_NS,
            labels={"app.kubernetes.io/name": svc_name, "managed-by": "kopf-operator"},
        ),
        spec=client.V1ServiceSpec(
            type="NodePort",
            selector={
                "app.kubernetes.io/name": "prometheus",
                "prometheus": "kube-prometheus-stack-prometheus",
            },
            ports=[
                client.V1ServicePort(
                    name="http-web",
                    port=9090,
                    target_port=9090,
                    node_port=node_port,
                    protocol="TCP",
                )
            ],
        ),
    )
    try:
        core_v1.create_namespaced_service(MONITORING_NS, svc_body)
        logger.info("Created NodePort Service '%s' on port %d in ns=%s ✓", svc_name, node_port, MONITORING_NS)
    except client.ApiException as exc:
        if exc.status != 409:  # Already exists is fine
            logger.warning("Could not create %s Service: %s", svc_name, exc)

