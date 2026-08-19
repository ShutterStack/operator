"""
platform_operator/main.py

Kopf Operator entrypoint.

Registers all handlers for:
  - PlatformStack   (deploy the full stack in dependency order)
  - PostgresInstance (standalone PostgreSQL)
  - IstioConfig      (standalone Istio)
  - MonitoringStack  (standalone Prometheus + Grafana)
  - AirflowInstance  (standalone Airflow)

Run locally (dev mode):
    source venv/bin/activate
    kopf run platform_operator/main.py --all-namespaces --dev

Run in-cluster (via Deployment):
    Dockerfile CMD: kopf run /app/platform_operator/main.py --all-namespaces
"""

import asyncio
import logging
import sys
from pathlib import Path

# Ensure project root (/app) is in sys.path regardless of how kopf is invoked
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import kopf

# Import all sub-handlers — this registers their @kopf.on.* decorators
import platform_operator.handlers  # noqa: F401


# PlatformStack-specific helpers
from platform_operator.handlers.istio      import deploy_istio_for_platform, teardown_istio, upgrade_istio_for_platform
from platform_operator.handlers.postgres   import deploy_postgres_for_platform, teardown_postgres, upgrade_postgres_for_platform
from platform_operator.handlers.monitoring import deploy_monitoring_for_platform, teardown_monitoring, upgrade_monitoring_for_platform
from platform_operator.handlers.airflow    import deploy_airflow_for_platform, teardown_airflow, upgrade_airflow_for_platform

logger = logging.getLogger(__name__)

GROUP   = "platform.ops"
VERSION = "v1alpha1"


# ─────────────────────────────────────────────────────────────────────────────
# Operator startup / teardown
# ─────────────────────────────────────────────────────────────────────────────

@kopf.on.startup()
def operator_startup(settings: kopf.OperatorSettings, **kwargs):
    """Configure Kopf operator global settings and print startup banner."""
    # Retry configuration
    settings.posting.level = logging.WARNING
    # Annotation key format: <dns-subdomain>/<name>  — only ONE slash allowed.
    # prefix must be a valid DNS subdomain (dots ok, slashes NOT ok).
    settings.persistence.finalizer = "kopf-finalizer.platform.ops"
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(
        prefix="kopf.platform.ops"
    )
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(
        prefix="kopf.platform.ops"
    )
    settings.watching.server_timeout = 60

    print("\n" + "=" * 70)
    print("  ✅ Kopf Platform Operator is RUNNING")
    print("=" * 70)
    print()
    print("  PHASE 1 complete — operator is idle, watching for your config.")
    print()
    print("  ▶  NEXT STEP — PHASE 2:")
    print("     1. Edit platform-config.yaml (fill in your VM IP if not done)")
    print("     2. Apply it:")
    print("        kubectl apply -f platform-config.yaml")
    print()
    print("  The operator will automatically detect the file and deploy:")
    print("  cert-manager → Istio (mTLS) → PostgreSQL → Grafana → Airflow")
    print()
    print("  ▶  WATCH DEPLOYMENT PROGRESS:")
    print("     kubectl get platformstack my-platform -n operator -w")
    print()
    print("  ▶  UIs (available after deployment):")
    print("     Grafana    →  https://192.168.56.50/grafana")
    print("     Airflow    →  https://192.168.56.50/airflow")
    print("     Prometheus →  https://192.168.56.50/prometheus")
    print("=" * 70 + "\n")

    logger.info("Kopf Platform Operator started ✓  |  K8s 1.36.x  |  Istio 1.24.2")


@kopf.on.cleanup()
def operator_cleanup(**kwargs):
    logger.info("Kopf Platform Operator shutting down...")


# ─────────────────────────────────────────────────────────────────────────────
# PlatformStack — full stack lifecycle
# ─────────────────────────────────────────────────────────────────────────────

@kopf.on.create(GROUP, VERSION, "platformstacks")
def platformstack_create(spec, name, namespace, patch, **kwargs):
    """
    Deploy the full platform stack in dependency order:
    cert-manager (pre-installed) → Istio → PostgreSQL → Monitoring → Airflow
    """
    logger.info("PlatformStack '%s' create triggered", name)
    patch.status["phase"] = "Starting"

    components = spec.get("components", {})

    # ── Phase 1: Istio (always first — sets up mTLS mesh) ────────────────────
    if components.get("istio", {}).get("enabled", True):
        patch.status["phase"] = "Deploying Istio"
        logger.info("[PlatformStack] Phase 1: Deploying Istio...")
        deploy_istio_for_platform(spec.get("istio", {}), patch)
        logger.info("[PlatformStack] Istio ready ✓")
    else:
        logger.warning("[PlatformStack] Istio disabled — mTLS mesh will NOT be active")

    # ── Phase 2: PostgreSQL ──────────────────────────────────────────────────
    if components.get("postgres", {}).get("enabled", True):
        patch.status["phase"] = "Deploying PostgreSQL"
        logger.info("[PlatformStack] Phase 2: Deploying PostgreSQL...")
        deploy_postgres_for_platform(spec.get("postgres", {}), patch)
        logger.info("[PlatformStack] PostgreSQL ready ✓")

    # ── Phase 3: Monitoring (Prometheus + Grafana) ───────────────────────────
    if components.get("monitoring", {}).get("enabled", True):
        patch.status["phase"] = "Deploying Monitoring"
        logger.info("[PlatformStack] Phase 3: Deploying Monitoring stack...")
        deploy_monitoring_for_platform(spec.get("monitoring", {}), patch)
        logger.info("[PlatformStack] Monitoring ready ✓")

    # ── Phase 4: Airflow (depends on PostgreSQL) ─────────────────────────────
    if components.get("airflow", {}).get("enabled", True):
        patch.status["phase"] = "Deploying Airflow"
        logger.info("[PlatformStack] Phase 4: Deploying Airflow...")
        deploy_airflow_for_platform(spec.get("airflow", {}), patch)
        logger.info("[PlatformStack] Airflow ready ✓")

    patch.status["phase"] = "Ready"
    patch.status["ready"] = True
    logger.info("PlatformStack '%s' fully deployed ✓", name)


@kopf.on.update(GROUP, VERSION, "platformstacks")
def platformstack_update(spec, name, old, new, diff, patch, **kwargs):
    """
    Upgrade individual components when PlatformStack spec changes.
    Only upgrades components whose spec section has changed.
    """
    logger.info("PlatformStack '%s' update triggered", name)
    patch.status["phase"] = "Upgrading"
    components = spec.get("components", {})

    changed_fields = {item[1][0] for item in diff if item[1]}

    if "istio" in changed_fields and components.get("istio", {}).get("enabled", True):
        logger.info("[PlatformStack] Upgrading Istio...")
        upgrade_istio_for_platform(spec.get("istio", {}), patch)

    if "postgres" in changed_fields and components.get("postgres", {}).get("enabled", True):
        logger.info("[PlatformStack] Upgrading PostgreSQL...")
        upgrade_postgres_for_platform(spec.get("postgres", {}), patch)

    if "monitoring" in changed_fields and components.get("monitoring", {}).get("enabled", True):
        logger.info("[PlatformStack] Upgrading Monitoring...")
        upgrade_monitoring_for_platform(spec.get("monitoring", {}), patch)

    if "airflow" in changed_fields and components.get("airflow", {}).get("enabled", True):
        logger.info("[PlatformStack] Upgrading Airflow...")
        upgrade_airflow_for_platform(spec.get("airflow", {}), patch)

    patch.status["phase"] = "Ready"
    logger.info("PlatformStack '%s' upgrade complete ✓", name)


@kopf.on.delete(GROUP, VERSION, "platformstacks")
def platformstack_delete(spec, name, patch, **kwargs):
    """
    Uninstall all components in reverse dependency order:
    Airflow → Monitoring → PostgreSQL → Istio
    """
    logger.info("PlatformStack '%s' delete triggered — tearing down all components...", name)
    components = spec.get("components", {})

    if components.get("airflow", {}).get("enabled", True):
        logger.info("[PlatformStack] Removing Airflow...")
        teardown_airflow()

    if components.get("monitoring", {}).get("enabled", True):
        logger.info("[PlatformStack] Removing Monitoring...")
        teardown_monitoring()

    if components.get("postgres", {}).get("enabled", True):
        logger.info("[PlatformStack] Removing PostgreSQL...")
        teardown_postgres(spec.get("postgres", {}))

    if components.get("istio", {}).get("enabled", True):
        logger.info("[PlatformStack] Removing Istio...")
        teardown_istio(spec.get("istio", {}))

    logger.info("PlatformStack '%s' fully removed ✓", name)


@kopf.on.timer(GROUP, VERSION, "platformstacks", interval=60.0, idle=30.0)
def platformstack_health_timer(spec, name, patch, **kwargs):
    """Aggregate health check across all components every 60 seconds."""
    from platform_operator.helm.helm_client import helm_is_deployed
    components = spec.get("components", {})

    health = {}
    if components.get("postgres", {}).get("enabled", True):
        from platform_operator.helm.health_check import get_pod_health
        health["postgres"] = get_pod_health(
            "app.kubernetes.io/name=postgresql", "postgres"
        )
    if components.get("monitoring", {}).get("enabled", True):
        from platform_operator.helm.health_check import get_pod_health
        health["prometheus"] = get_pod_health(
            "app.kubernetes.io/name=prometheus", "monitoring"
        )
        health["grafana"] = get_pod_health(
            "app.kubernetes.io/name=grafana", "monitoring"
        )
    if components.get("airflow", {}).get("enabled", True):
        from platform_operator.helm.health_check import get_pod_health
        health["airflow-webserver"] = get_pod_health("component=webserver", "airflow")
    if components.get("istio", {}).get("enabled", True):
        from platform_operator.helm.health_check import get_pod_health
        health["istiod"] = get_pod_health("app=istiod", "istio-system")

    patch.status["componentHealth"] = health
    all_running = all(
        v.get("running", 0) > 0 for v in health.values() if v.get("total", 0) > 0
    )
    patch.status["ready"] = all_running
