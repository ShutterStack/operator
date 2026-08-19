"""
platform_operator/tls/istio_tls.py
Istio-centric TLS management module.

Design:
  - cert-manager creates ONE ClusterIssuer (self-signed CA) and ONE Certificate
    (for the Istio Ingress Gateway only).
  - All east-west service-to-service encryption is handled by Istio's mTLS
    via PeerAuthentication (STRICT) and DestinationRules (ISTIO_MUTUAL).
  - Individual app namespaces (postgres, monitoring, airflow) have NO certs.
  - Apps run plain HTTP internally; Envoy sidecars transparently handle mTLS.
"""

import logging
import time
from typing import List, Optional

from kubernetes import client
from kubernetes.client.rest import ApiException

from platform_operator.helm.health_check import _ensure_k8s, wait_for_cert_ready

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# cert-manager resources
# ---------------------------------------------------------------------------

def create_self_signed_cluster_issuer(issuer_name: str = "platform-selfsigned-issuer") -> None:
    """
    Create a cert-manager ClusterIssuer backed by a self-signed CA.
    Two-step:
      1. ClusterIssuer (selfSigned) → used to sign the CA certificate
      2. CA Certificate → creates a Secret with the CA key/cert
      3. ClusterIssuer (ca) → used to sign workload certs (gateway only)
    """
    _ensure_k8s()
    custom = client.CustomObjectsApi()

    # Step 1: Bootstrap self-signed issuer
    _apply_custom_resource(custom, {
        "apiVersion": "cert-manager.io/v1",
        "kind": "ClusterIssuer",
        "metadata": {"name": "selfsigned-bootstrap"},
        "spec": {"selfSigned": {}},
    }, group="cert-manager.io", version="v1", plural="clusterissuers", cluster_scoped=True)

    # Step 2: CA certificate signed by bootstrap issuer
    _apply_custom_resource(custom, {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {"name": "platform-ca", "namespace": "cert-manager"},
        "spec": {
            "isCA": True,
            "commonName": "platform-ca",
            "secretName": "platform-ca-secret",
            "privateKey": {"algorithm": "ECDSA", "size": 256},
            "issuerRef": {
                "name": "selfsigned-bootstrap",
                "kind": "ClusterIssuer",
                "group": "cert-manager.io",
            },
        },
    }, group="cert-manager.io", version="v1", plural="certificates",
       namespace="cert-manager")

    # Wait for CA cert to be ready
    wait_for_cert_ready("platform-ca", "cert-manager", timeout=60)

    # Step 3: Real ClusterIssuer backed by the CA
    _apply_custom_resource(custom, {
        "apiVersion": "cert-manager.io/v1",
        "kind": "ClusterIssuer",
        "metadata": {"name": issuer_name},
        "spec": {
            "ca": {"secretName": "platform-ca-secret"}
        },
    }, group="cert-manager.io", version="v1", plural="clusterissuers", cluster_scoped=True)

    logger.info("ClusterIssuer '%s' is ready ✓", issuer_name)


def create_gateway_certificate(
    hosts: List[str],
    ip_sans: Optional[List[str]] = None,
    issuer_name: str = "platform-selfsigned-issuer",
    cert_name: str = "istio-gateway-tls",
    secret_name: str = "istio-gateway-tls",
    namespace: str = "istio-system",
) -> None:
    """
    Create ONE cert-manager Certificate for the Istio Ingress Gateway.
    This is the ONLY certificate created by the operator for TLS.
    All internal mTLS is handled by Istio automatically.
    """
    _ensure_k8s()
    custom = client.CustomObjectsApi()

    cert_body: dict = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {"name": cert_name, "namespace": namespace},
        "spec": {
            "secretName": secret_name,
            "commonName": hosts[0] if hosts else "platform.local",
            "dnsNames": hosts,
            "issuerRef": {
                "name": issuer_name,
                "kind": "ClusterIssuer",
                "group": "cert-manager.io",
            },
            "privateKey": {"algorithm": "ECDSA", "size": 256},
        },
    }
    if ip_sans:
        cert_body["spec"]["ipAddresses"] = ip_sans

    _apply_custom_resource(
        custom, cert_body,
        group="cert-manager.io", version="v1", plural="certificates",
        namespace=namespace,
    )
    logger.info("Gateway Certificate '%s' created in ns=%s", cert_name, namespace)


# ---------------------------------------------------------------------------
# Namespace labeling (Istio sidecar injection)
# ---------------------------------------------------------------------------

def label_namespace_for_injection(namespace: str) -> None:
    """Add istio-injection=enabled label to a namespace."""
    _ensure_k8s()
    core_v1 = client.CoreV1Api()
    try:
        ns = core_v1.read_namespace(name=namespace)
        labels = ns.metadata.labels or {}
        if labels.get("istio-injection") == "enabled":
            logger.debug("Namespace '%s' already labeled for Istio injection", namespace)
            return
        labels["istio-injection"] = "enabled"
        ns.metadata.labels = labels
        core_v1.patch_namespace(name=namespace, body=ns)
        logger.info("Labeled namespace '%s' with istio-injection=enabled ✓", namespace)
    except ApiException as exc:
        logger.error("Failed to label namespace '%s': %s", namespace, exc)
        raise


def remove_injection_label(namespace: str) -> None:
    """Remove the istio-injection label from a namespace on cleanup."""
    _ensure_k8s()
    core_v1 = client.CoreV1Api()
    try:
        ns = core_v1.read_namespace(name=namespace)
        labels = ns.metadata.labels or {}
        if "istio-injection" in labels:
            labels.pop("istio-injection")
            ns.metadata.labels = labels
            core_v1.patch_namespace(name=namespace, body=ns)
            logger.info("Removed istio-injection label from namespace '%s'", namespace)
    except ApiException:
        pass  # Best-effort on cleanup


# ---------------------------------------------------------------------------
# PeerAuthentication — STRICT mTLS mesh-wide
# ---------------------------------------------------------------------------

def apply_peer_authentication_strict(namespace: str = "istio-system") -> None:
    """
    Apply a mesh-wide PeerAuthentication resource that enforces STRICT mTLS.
    Placing it in istio-system applies it globally across the entire mesh.
    """
    _ensure_k8s()
    custom = client.CustomObjectsApi()
    body = {
        "apiVersion": "security.istio.io/v1beta1",
        "kind": "PeerAuthentication",
        "metadata": {
            "name": "mesh-mtls-strict",
            "namespace": namespace,
        },
        "spec": {
            "mtls": {"mode": "STRICT"}
        },
    }
    _apply_custom_resource(
        custom, body,
        group="security.istio.io", version="v1beta1",
        plural="peerauthentications", namespace=namespace,
    )
    logger.info("PeerAuthentication (STRICT mTLS) applied mesh-wide ✓")


# ---------------------------------------------------------------------------
# DestinationRule — ISTIO_MUTUAL per namespace
# ---------------------------------------------------------------------------

def apply_destination_rule(namespace: str) -> None:
    """
    Apply a DestinationRule in the given namespace so that Envoy sidecars
    in that namespace use ISTIO_MUTUAL (mTLS) when communicating with
    any service in that namespace.
    """
    _ensure_k8s()
    custom = client.CustomObjectsApi()
    body = {
        "apiVersion": "networking.istio.io/v1beta1",
        "kind": "DestinationRule",
        "metadata": {
            "name": f"{namespace}-mtls-dr",
            "namespace": namespace,
        },
        "spec": {
            "host": f"*.{namespace}.svc.cluster.local",
            "trafficPolicy": {
                "tls": {"mode": "ISTIO_MUTUAL"}
            },
        },
    }
    _apply_custom_resource(
        custom, body,
        group="networking.istio.io", version="v1beta1",
        plural="destinationrules", namespace=namespace,
    )
    logger.info("DestinationRule (ISTIO_MUTUAL) applied in namespace '%s' ✓", namespace)


# ---------------------------------------------------------------------------
# Istio Gateway — single platform-wide HTTPS Gateway
# ---------------------------------------------------------------------------

def apply_platform_gateway(
    gateway_cert_secret: str = "istio-gateway-tls",
    gateway_namespace: str = "istio-system",
) -> None:
    """
    Create or update the Istio Gateway resource that terminates HTTPS
    at the Ingress Gateway. All services are exposed through this single Gateway.
    HTTP traffic is redirected to HTTPS.
    """
    _ensure_k8s()
    custom = client.CustomObjectsApi()
    body = {
        "apiVersion": "networking.istio.io/v1beta1",
        "kind": "Gateway",
        "metadata": {
            "name": "platform-gateway",
            "namespace": gateway_namespace,
        },
        "spec": {
            "selector": {"istio": "gateway"},
            "servers": [
                {
                    "port": {"number": 443, "name": "https", "protocol": "HTTPS"},
                    "tls": {
                        "mode": "SIMPLE",
                        "credentialName": gateway_cert_secret,
                    },
                    "hosts": ["*"],
                },
                {
                    "port": {"number": 80, "name": "http", "protocol": "HTTP"},
                    "hosts": ["*"],
                    "tls": {"httpsRedirect": True},
                },
            ],
        },
    }
    _apply_custom_resource(
        custom, body,
        group="networking.istio.io", version="v1beta1",
        plural="gateways", namespace=gateway_namespace,
    )
    logger.info("Platform Gateway applied (TLS via secret '%s') ✓", gateway_cert_secret)


# ---------------------------------------------------------------------------
# VirtualService — per-service routing through Gateway
# ---------------------------------------------------------------------------

def apply_virtual_service(
    name: str,
    service_name: str,
    service_namespace: str,
    service_port: int,
    uri_prefix: str,
    gateway_namespace: str = "istio-system",
) -> None:
    """
    Create an Istio VirtualService that routes requests entering via the
    platform Gateway to an internal service by URI prefix.

    Example: /grafana → grafana.monitoring.svc.cluster.local:80
    """
    _ensure_k8s()
    custom = client.CustomObjectsApi()
    body = {
        "apiVersion": "networking.istio.io/v1beta1",
        "kind": "VirtualService",
        "metadata": {
            "name": name,
            "namespace": gateway_namespace,
        },
        "spec": {
            "hosts": ["*"],
            "gateways": [f"{gateway_namespace}/platform-gateway"],
            "http": [
                {
                    "match": [{"uri": {"prefix": uri_prefix}}],
                    "route": [
                        {
                            "destination": {
                                "host": f"{service_name}.{service_namespace}.svc.cluster.local",
                                "port": {"number": service_port},
                            }
                        }
                    ],
                }
            ],
        },
    }
    _apply_custom_resource(
        custom, body,
        group="networking.istio.io", version="v1beta1",
        plural="virtualservices", namespace=gateway_namespace,
    )
    logger.info(
        "VirtualService '%s' → %s/%s:%d (prefix=%s) ✓",
        name, service_namespace, service_name, service_port, uri_prefix,
    )


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

def delete_istio_resources(namespaces: List[str], gateway_namespace: str = "istio-system") -> None:
    """Remove all Istio resources created by this operator."""
    _ensure_k8s()
    custom = client.CustomObjectsApi()

    # Delete PeerAuthentication
    _delete_custom_resource(custom, "security.istio.io", "v1beta1", "peerauthentications",
                            "mesh-mtls-strict", gateway_namespace)

    # Delete Gateway
    _delete_custom_resource(custom, "networking.istio.io", "v1beta1", "gateways",
                            "platform-gateway", gateway_namespace)

    # Delete VirtualServices
    for vs_name in ["grafana-vs", "airflow-vs"]:
        _delete_custom_resource(custom, "networking.istio.io", "v1beta1", "virtualservices",
                                vs_name, gateway_namespace)

    # Delete DestinationRules and remove namespace labels
    for ns in namespaces:
        _delete_custom_resource(custom, "networking.istio.io", "v1beta1", "destinationrules",
                                f"{ns}-mtls-dr", ns)
        remove_injection_label(ns)

    # Delete gateway certificate
    _delete_custom_resource(custom, "cert-manager.io", "v1", "certificates",
                            "istio-gateway-tls", gateway_namespace)

    logger.info("Istio resources cleaned up ✓")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _apply_custom_resource(
    custom: client.CustomObjectsApi,
    body: dict,
    group: str,
    version: str,
    plural: str,
    namespace: Optional[str] = None,
    cluster_scoped: bool = False,
) -> None:
    """Create or update (server-side apply via replace) a custom resource."""
    name = body["metadata"]["name"]
    try:
        if cluster_scoped:
            existing = custom.get_cluster_custom_object(group, version, plural, name)
            body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
            custom.replace_cluster_custom_object(group, version, plural, name, body)
        else:
            existing = custom.get_namespaced_custom_object(group, version, namespace, plural, name)
            body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
            custom.replace_namespaced_custom_object(group, version, namespace, plural, name, body)
        logger.debug("Updated %s '%s'", body["kind"], name)
    except ApiException as exc:
        if exc.status == 404:
            if cluster_scoped:
                custom.create_cluster_custom_object(group, version, plural, body)
            else:
                custom.create_namespaced_custom_object(group, version, namespace, plural, body)
            logger.debug("Created %s '%s'", body["kind"], name)
        else:
            raise


def _delete_custom_resource(
    custom: client.CustomObjectsApi,
    group: str,
    version: str,
    plural: str,
    name: str,
    namespace: str,
) -> None:
    """Delete a namespaced custom resource, ignoring 404."""
    try:
        custom.delete_namespaced_custom_object(group, version, namespace, plural, name)
        logger.debug("Deleted %s '%s' in ns=%s", plural, name, namespace)
    except ApiException as exc:
        if exc.status != 404:
            logger.warning("Could not delete %s '%s': %s", plural, name, exc)
