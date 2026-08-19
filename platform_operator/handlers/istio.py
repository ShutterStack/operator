"""
platform_operator/handlers/istio.py

Kopf handler for the IstioConfig CRD and the Istio sub-component
of PlatformStack. Responsible for:

  1. Installing Istio (base + istiod + ingress gateway) via Helm
  2. Labeling target namespaces for sidecar injection
  3. Applying mesh-wide STRICT mTLS (PeerAuthentication)
  4. Applying ISTIO_MUTUAL DestinationRules per namespace
  5. Creating the ONE gateway TLS certificate (cert-manager)
  6. Creating the platform-wide Istio Gateway + VirtualServices
  7. Health-checking via timer
  8. Graceful Helm uninstall + Istio resource cleanup on delete
"""

import logging

import kopf

from platform_operator.config.defaults import (
    CHARTS,
    CHART_VERSIONS,
    ISTIO_BASE_DEFAULTS,
    ISTIOD_DEFAULTS,
    ISTIO_GATEWAY_DEFAULTS,
    MANAGED_NAMESPACES,
)
from platform_operator.helm.helm_client import (
    HelmError,
    helm_install,
    helm_is_deployed,
    helm_uninstall,
    helm_upgrade,
)
from platform_operator.helm.health_check import (
    ensure_namespace,
    get_pod_health,
    wait_for_deployment_ready,
)
from platform_operator.tls.istio_tls import (
    apply_destination_rule,
    apply_peer_authentication_strict,
    apply_platform_gateway,
    apply_virtual_service,
    create_gateway_certificate,
    create_self_signed_cluster_issuer,
    delete_istio_resources,
    label_namespace_for_injection,
    wait_for_cert_ready,
)

logger = logging.getLogger(__name__)

GROUP = "platform.ops"
VERSION = "v1alpha1"


# ─────────────────────────────────────────────────────────────────────────────
# Standalone IstioConfig CRD handlers
# ─────────────────────────────────────────────────────────────────────────────

@kopf.on.create(GROUP, VERSION, "istioconfigs")
def istio_create(spec, name, namespace, patch, **kwargs):
    """Deploy Istio control plane + configure mTLS mesh when an IstioConfig CR is created."""
    logger.info("IstioConfig '%s' created — deploying Istio stack...", name)
    patch.status["phase"] = "Installing"
    _deploy_istio(spec, patch)


@kopf.on.update(GROUP, VERSION, "istioconfigs")
def istio_update(spec, name, old, new, patch, **kwargs):
    """Upgrade Istio if spec changes."""
    logger.info("IstioConfig '%s' updated — upgrading Istio stack...", name)
    patch.status["phase"] = "Upgrading"
    _upgrade_istio(spec, patch)


@kopf.on.delete(GROUP, VERSION, "istioconfigs")
def istio_delete(spec, name, patch, **kwargs):
    """Uninstall Istio and remove all mesh resources."""
    logger.info("IstioConfig '%s' deleted — tearing down Istio...", name)
    _teardown_istio(spec)


@kopf.on.timer(GROUP, VERSION, "istioconfigs", interval=60.0, idle=30.0)
def istio_health_timer(spec, name, patch, **kwargs):
    """Periodic health check for Istio deployments."""
    health = _check_istio_health()
    patch.status["health"] = health
    patch.status["mtls"] = "STRICT" if health.get("istiod", {}).get("running", 0) > 0 else "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────────────
# Shared install logic (called by both standalone CRD and PlatformStack)
# ─────────────────────────────────────────────────────────────────────────────

def deploy_istio_for_platform(spec: dict, patch) -> None:
    """Entry point called by the PlatformStack handler."""
    _deploy_istio(spec, patch)


def upgrade_istio_for_platform(spec: dict, patch) -> None:
    _upgrade_istio(spec, patch)


def teardown_istio(spec: dict) -> None:
    _teardown_istio(spec)


# ─────────────────────────────────────────────────────────────────────────────
# Internal implementation
# ─────────────────────────────────────────────────────────────────────────────

def _deploy_istio(spec: dict, patch) -> None:
    istio_ns = "istio-system"
    ensure_namespace(istio_ns)

    gw_hosts = spec.get("gatewayHosts", ["platform.local"])
    ip_sans  = spec.get("gatewayIpSans", [])

    try:
        # ── Step 1: Helm — install/upgrade istio/base ────────────────────────
        patch.status["phase"] = "Deploying istio/base"
        helm_upgrade(
            release="istio-base",
            chart=CHARTS["istio_base"],
            namespace=istio_ns,
            version=CHART_VERSIONS["istio_base"],
            set_values=ISTIO_BASE_DEFAULTS,
            install=True,
            timeout="8m",
        )
        logger.info("istio/base deployed ✓")

        # ── Step 2: Helm — install/upgrade istiod ────────────────────────────
        patch.status["phase"] = "Deploying istiod"
        helm_upgrade(
            release="istiod",
            chart=CHARTS["istiod"],
            namespace=istio_ns,
            version=CHART_VERSIONS["istiod"],
            set_values=ISTIOD_DEFAULTS,
            install=True,
            timeout="8m",
        )
        wait_for_deployment_ready("istiod", istio_ns, timeout=300)
        logger.info("istiod deployed ✓")

        # ── Step 3: Helm — install/upgrade ingress gateway ───────────────────
        patch.status["phase"] = "Deploying Ingress Gateway"
        
        gw_values = dict(ISTIO_GATEWAY_DEFAULTS)
        if gw_hosts:
            gw_values["service.externalIPs[0]"] = gw_hosts[0]
            
        helm_upgrade(
            release="istio-gateway",
            chart=CHARTS["istio_gateway"],
            namespace=istio_ns,
            version=CHART_VERSIONS["istio_gateway"],
            set_values=gw_values,
            install=True,
            timeout="8m",
        )
        wait_for_deployment_ready("istio-gateway", istio_ns, timeout=180)
        logger.info("Istio Ingress Gateway deployed ✓")

        # ── Step 4: cert-manager (ensure installed) & ClusterIssuer ─────────
        patch.status["phase"] = "Ensuring cert-manager is installed"
        helm_upgrade(
            release="cert-manager",
            chart=CHARTS["cert_manager"],
            namespace="cert-manager",
            version=CHART_VERSIONS["cert_manager"],
            set_values={
                "crds.enabled": "true",
                "crds.keep": "true",
                "global.leaderElection.namespace": "cert-manager",
                "securityContext.runAsNonRoot": "true",
                "cainjector.securityContext.runAsNonRoot": "true",
                "webhook.securityContext.runAsNonRoot": "true",
            },
            install=True,
            timeout="8m",
        )
        wait_for_deployment_ready("cert-manager", "cert-manager", timeout=180)
        wait_for_deployment_ready("cert-manager-webhook", "cert-manager", timeout=180)
        logger.info("cert-manager deployed and ready ✓")


        patch.status["phase"] = "Creating cert-manager CA"
        create_self_signed_cluster_issuer()


        # ── Step 5: Gateway TLS certificate ─────────────────────────────────
        patch.status["phase"] = "Creating Gateway TLS certificate"
        create_gateway_certificate(hosts=gw_hosts, ip_sans=ip_sans)
        wait_for_cert_ready("istio-gateway-tls", istio_ns, timeout=120)
        logger.info("Gateway TLS certificate ready ✓")

        # ── Step 6: Label namespaces for sidecar injection ───────────────────
        patch.status["phase"] = "Labeling namespaces for mTLS"
        for ns in MANAGED_NAMESPACES:
            ensure_namespace(ns)
            label_namespace_for_injection(ns)

        # ── Step 7: PeerAuthentication STRICT mesh-wide ──────────────────────
        apply_peer_authentication_strict()

        # ── Step 8: DestinationRule per namespace ────────────────────────────
        for ns in MANAGED_NAMESPACES:
            apply_destination_rule(ns)

        # ── Step 9: Platform Gateway ─────────────────────────────────────────
        apply_platform_gateway()

        # ── Step 10: VirtualServices ─────────────────────────────────────────
        apply_virtual_service(
            name="grafana-vs",
            service_name="kube-prometheus-stack-grafana",
            service_namespace="monitoring",
            service_port=80,
            uri_prefix="/grafana",
        )
        apply_virtual_service(
            name="airflow-vs",
            service_name="airflow-webserver",
            service_namespace="airflow",
            service_port=8080,
            uri_prefix="/airflow",
        )

        patch.status["phase"] = "Ready"
        patch.status["mtls"] = "STRICT"
        patch.status["gatewayHosts"] = gw_hosts
        logger.info("Istio stack fully deployed with mTLS mesh ✓")

    except HelmError as exc:
        patch.status["phase"] = "Error"
        patch.status["error"] = str(exc)
        raise kopf.TemporaryError(f"Helm error during Istio deploy: {exc}", delay=60)
    except TimeoutError as exc:
        patch.status["phase"] = "Error"
        patch.status["error"] = str(exc)
        raise kopf.TemporaryError(str(exc), delay=90)


def _upgrade_istio(spec: dict, patch) -> None:
    istio_ns = "istio-system"

    try:
        helm_upgrade(
            release="istiod",
            chart=CHARTS["istiod"],
            namespace=istio_ns,
            version=CHART_VERSIONS["istiod"],
            set_values=ISTIOD_DEFAULTS,   # no 'profile' — that's istioctl only
        )
        wait_for_deployment_ready("istiod", istio_ns, timeout=300)

        gw_hosts = spec.get("gatewayHosts", ["platform.local"])
        gw_values = dict(ISTIO_GATEWAY_DEFAULTS)
        if gw_hosts:
            gw_values["service.externalIPs[0]"] = gw_hosts[0]

        helm_upgrade(
            release="istio-gateway",
            chart=CHARTS["istio_gateway"],
            namespace=istio_ns,
            version=CHART_VERSIONS["istio_gateway"],
            set_values=gw_values,
        )
        wait_for_deployment_ready("istio-gateway", istio_ns, timeout=180)

        # Re-apply Istio mesh resources in case hosts changed
        gw_hosts = spec.get("gatewayHosts", ["platform.local"])
        ip_sans  = spec.get("gatewayIpSans", [])
        create_gateway_certificate(hosts=gw_hosts, ip_sans=ip_sans)
        apply_platform_gateway()

        patch.status["phase"] = "Ready"
        logger.info("Istio upgraded ✓")
    except (HelmError, TimeoutError) as exc:
        patch.status["phase"] = "Error"
        raise kopf.TemporaryError(str(exc), delay=60)


def _teardown_istio(spec: dict) -> None:
    istio_ns = "istio-system"
    # Remove mesh resources first (VirtualServices, Gateway, DestinationRules, PeerAuth, Certs)
    delete_istio_resources(MANAGED_NAMESPACES)
    # Uninstall Helm releases in reverse dependency order
    for release in ["istio-gateway", "istiod", "istio-base"]:
        helm_uninstall(release, istio_ns, ignore_not_found=True)
    logger.info("Istio teardown complete ✓")


def _check_istio_health() -> dict:
    istiod_health = get_pod_health("app=istiod", "istio-system")
    gw_health     = get_pod_health("app=istio-gateway", "istio-system")
    return {
        "istiod":         istiod_health,
        "ingressGateway": gw_health,
    }
