"""
platform_operator/helm/health_check.py
Kubernetes readiness polling utilities used by all handlers
to gate progression between deployment phases.
"""

import logging
import time
from typing import Optional

from kubernetes import client, config as k8s_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kubernetes client init (handles both in-cluster and out-of-cluster)
# ---------------------------------------------------------------------------
_k8s_initialized = False


def _ensure_k8s() -> None:
    global _k8s_initialized
    if not _k8s_initialized:
        try:
            k8s_config.load_incluster_config()
            logger.debug("Loaded in-cluster kubeconfig")
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
            logger.debug("Loaded local kubeconfig")
        _k8s_initialized = True


# ---------------------------------------------------------------------------
# Deployment readiness
# ---------------------------------------------------------------------------

def wait_for_deployment_ready(
    name: str,
    namespace: str,
    timeout: int = 300,
    poll_interval: int = 10,
) -> bool:
    """
    Poll until all desired replicas of a Deployment are available.
    Returns True on success, raises TimeoutError on timeout.
    """
    _ensure_k8s()
    apps_v1 = client.AppsV1Api()
    deadline = time.time() + timeout

    logger.info("Waiting for Deployment %s/%s to be ready (timeout=%ds)...", namespace, name, timeout)
    while time.time() < deadline:
        try:
            deploy = apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
            desired   = deploy.spec.replicas or 1
            available = deploy.status.available_replicas or 0
            ready     = deploy.status.ready_replicas or 0

            logger.debug(
                "Deployment %s/%s: desired=%d available=%d ready=%d",
                namespace, name, desired, available, ready,
            )
            if available >= desired and ready >= desired:
                logger.info("Deployment %s/%s is ready ✓", namespace, name)
                return True
        except client.ApiException as exc:
            if exc.status == 404:
                logger.debug("Deployment %s/%s not found yet, retrying...", namespace, name)
            else:
                raise
        time.sleep(poll_interval)

    raise TimeoutError(
        f"Deployment {namespace}/{name} did not become ready within {timeout}s"
    )


# ---------------------------------------------------------------------------
# StatefulSet readiness
# ---------------------------------------------------------------------------

def wait_for_statefulset_ready(
    name: str,
    namespace: str,
    timeout: int = 300,
    poll_interval: int = 10,
) -> bool:
    """
    Poll until all desired replicas of a StatefulSet are ready.
    """
    _ensure_k8s()
    apps_v1 = client.AppsV1Api()
    deadline = time.time() + timeout

    logger.info("Waiting for StatefulSet %s/%s to be ready (timeout=%ds)...", namespace, name, timeout)
    while time.time() < deadline:
        try:
            ss = apps_v1.read_namespaced_stateful_set(name=name, namespace=namespace)
            desired = ss.spec.replicas or 1
            ready   = ss.status.ready_replicas or 0

            logger.debug("StatefulSet %s/%s: desired=%d ready=%d", namespace, name, desired, ready)
            if ready >= desired:
                logger.info("StatefulSet %s/%s is ready ✓", namespace, name)
                return True
        except client.ApiException as exc:
            if exc.status == 404:
                logger.debug("StatefulSet %s/%s not found yet, retrying...", namespace, name)
            else:
                raise
        time.sleep(poll_interval)

    raise TimeoutError(
        f"StatefulSet {namespace}/{name} did not become ready within {timeout}s"
    )


# ---------------------------------------------------------------------------
# Pod health snapshot
# ---------------------------------------------------------------------------

def get_pod_health(label_selector: str, namespace: str) -> dict:
    """
    Return a dict summarising pod health for a given label selector.
    Example: {'total': 3, 'running': 3, 'pending': 0, 'failed': 0}
    """
    _ensure_k8s()
    core_v1 = client.CoreV1Api()
    try:
        pods = core_v1.list_namespaced_pod(
            namespace=namespace, label_selector=label_selector
        )
    except client.ApiException:
        return {"total": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}

    counts: dict = {"total": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}
    for pod in pods.items:
        phase = (pod.status.phase or "Unknown").lower()
        counts["total"] += 1
        counts[phase if phase in counts else "unknown"] += 1
    return counts


# ---------------------------------------------------------------------------
# CertificateRequest / cert-manager readiness
# ---------------------------------------------------------------------------

def wait_for_cert_ready(
    name: str,
    namespace: str,
    timeout: int = 120,
    poll_interval: int = 5,
) -> bool:
    """
    Wait until a cert-manager Certificate reaches 'Ready=True'.
    Uses the custom objects API to read cert-manager CRs.
    """
    _ensure_k8s()
    custom = client.CustomObjectsApi()
    deadline = time.time() + timeout

    logger.info("Waiting for Certificate %s/%s to be ready...", namespace, name)
    while time.time() < deadline:
        try:
            cert = custom.get_namespaced_custom_object(
                group="cert-manager.io",
                version="v1",
                namespace=namespace,
                plural="certificates",
                name=name,
            )
            conditions = cert.get("status", {}).get("conditions", [])
            for cond in conditions:
                if cond.get("type") == "Ready" and cond.get("status") == "True":
                    logger.info("Certificate %s/%s is Ready ✓", namespace, name)
                    return True
        except client.ApiException as exc:
            if exc.status == 404:
                logger.debug("Certificate %s/%s not found yet...", namespace, name)
            else:
                raise
        time.sleep(poll_interval)

    raise TimeoutError(
        f"Certificate {namespace}/{name} did not become Ready within {timeout}s"
    )


# ---------------------------------------------------------------------------
# Namespace existence
# ---------------------------------------------------------------------------

PLATFORM_NS_LABELS = {
    "platform.ops/managed-by": "kopf-operator",
    "app.kubernetes.io/managed-by": "platform-operator",
}


def ensure_namespace(namespace: str, extra_labels: dict | None = None) -> None:
    """
    Create namespace if it does not exist and stamp it with platform labels.

    Namespaces created by the operator are labelled with
    platform.ops/managed-by=kopf-operator so teardown handlers know which
    namespaces they own.  Helm resources policy keeps the Helm-chart-created
    namespaces alive across helm uninstall; these labels are additive.
    """
    _ensure_k8s()
    core_v1 = client.CoreV1Api()

    labels = {**PLATFORM_NS_LABELS, **(extra_labels or {})}

    try:
        ns = core_v1.read_namespace(name=namespace)
        # Patch labels onto existing namespace (idempotent)
        existing_labels = ns.metadata.labels or {}
        merged = {**existing_labels, **labels}
        if merged != existing_labels:
            core_v1.patch_namespace(
                name=namespace,
                body={"metadata": {"labels": merged}},
            )
            logger.debug("Patched labels onto existing namespace '%s'", namespace)
        else:
            logger.debug("Namespace '%s' already exists with correct labels", namespace)
    except client.ApiException as exc:
        if exc.status == 404:
            core_v1.create_namespace(
                body=client.V1Namespace(
                    metadata=client.V1ObjectMeta(
                        name=namespace,
                        labels=labels,
                    )
                )
            )
            logger.info("Created namespace '%s' with platform labels", namespace)
        else:
            raise

