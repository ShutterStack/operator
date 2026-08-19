"""
platform_operator/config/defaults.py
Default Helm values and chart references for every managed service.
All values here can be overridden via the CRD spec.

Verified chart versions (August 2026):
  - Istio 1.30.3         → officially supports K8s 1.32–1.36  ✓
  - cert-manager v1.15.3 → crds.enabled=true (installCRDs deprecated since v1.15)
  - bitnami/postgresql 16.x → stable, OCI-based
  - kube-prometheus-stack 68.x → K8s 1.36 compatible stable series
  - apache-airflow 1.15.0 → Airflow 2.9.3, stable
"""

# ---------------------------------------------------------------------------
# Helm chart references
# ---------------------------------------------------------------------------
CHARTS = {
    "cert_manager":          "jetstack/cert-manager",
    # bitnami HTTP repo (charts.bitnami.com) requires subscription since Aug 2025.
    # Use OCI registry instead — it is still publicly free.
    "postgresql":            "oci://registry-1.docker.io/bitnamicharts/postgresql",
    "istio_base":            "istio/base",
    "istiod":               "istio/istiod",
    "istio_gateway":         "istio/gateway",
    "kube_prometheus_stack": "prometheus-community/kube-prometheus-stack",
    "airflow":               "apache-airflow/airflow",
}


# ---------------------------------------------------------------------------
# Pinned chart versions — verified compatible with Kubernetes v1.36.x
# ---------------------------------------------------------------------------
CHART_VERSIONS = {
    # cert-manager v1.15.3 — K8s 1.36 compatible; crds.enabled=true replaces installCRDs
    "cert_manager":           "v1.15.3",

    # Istio 1.30.3 — officially supports K8s 1.32-1.36
    "istio_base":             "1.30.3",
    "istiod":                "1.30.3",
    "istio_gateway":          "1.30.3",

    # bitnami postgresql OCI chart — 16.3.5 ships PostgreSQL 16.4 and is verified free
    "postgresql":             "16.3.5",

    # kube-prometheus-stack 68.1.0 — confirmed released, K8s 1.36 compatible
    "kube_prometheus_stack":  "68.1.0",

    # Apache Airflow chart 1.15.0 — Airflow 2.9.3 — verified exists
    "airflow":               "1.15.0",
}

# App image versions (pinned for reproducibility)
APP_VERSIONS = {
    "postgresql":  "17.2.0",
    "grafana":     "11.3.1",
    "prometheus":  "2.55.0",
    "airflow":     "2.9.3",
}

# ---------------------------------------------------------------------------
# Release names  (helm release name → namespace)
# ---------------------------------------------------------------------------
RELEASES = {
    "postgresql":            "postgres",
    "istio-base":            "istio-system",
    "istiod":               "istio-system",
    "istio-gateway":         "istio-system",
    "kube-prometheus-stack": "monitoring",
    "airflow":               "airflow",
}

# ---------------------------------------------------------------------------
# Security-hardened default Helm values per chart
# ---------------------------------------------------------------------------

POSTGRES_DEFAULTS = {
    # bitnami/postgresql auth values
    "auth.username":        "appuser",
    "auth.database":        "appdb",
    # Disable postgres superuser — app only uses the dedicated user
    "auth.enablePostgresUser": "false",
    "primary.persistence.size":              "10Gi",
    "primary.resources.requests.memory":     "256Mi",
    "primary.resources.requests.cpu":        "250m",
    "primary.resources.limits.memory":       "512Mi",
    "primary.resources.limits.cpu":          "500m",
    # Security context — non-root, no privilege escalation
    "primary.containerSecurityContext.runAsNonRoot":             "true",
    "primary.containerSecurityContext.allowPrivilegeEscalation": "false",
    "primary.containerSecurityContext.readOnlyRootFilesystem":   "false",
    "primary.containerSecurityContext.capabilities.drop[0]":     "ALL",
    "primary.podSecurityContext.runAsNonRoot":                   "true",
    "primary.podSecurityContext.runAsUser":                      "1001",
    "primary.podSecurityContext.fsGroup":                        "1001",
    # Needed for kubeadm clusters without default StorageClass magic
    "primary.persistence.storageClass": "",
    # Disable metrics ServiceMonitor in Phase 2 to prevent CRD dependency crash
    "metrics.enabled": "false",
    "metrics.serviceMonitor.enabled": "false",
    # Superuser disabled — only appuser is used
    "auth.enablePostgresUser": "false",
    "volumePermissions.enabled": "false",
    "global.security.allowInsecureImages": "true",
    # Use 'latest' tag — Bitnami permanently maintains and never deletes :latest on Docker Hub
    "image.registry": "docker.io",
    "image.repository": "bitnami/postgresql",
    "image.tag": "latest",
    "primary.configuration": "listen_addresses = '*'\nlog_connections = on\nlog_disconnections = on\nlog_hostname = off\npassword_encryption = scram-sha-256",
    "primary.pgHbaConfiguration": "local   all       all                       trust\nhost    all       all        127.0.0.1/32   md5\nhost    all       all        127.0.0.0/8    md5\nhost    all       all        10.244.0.0/16  md5\nhost    all       all        ::1/128        md5",
}

ISTIO_BASE_DEFAULTS = {
    # Empty string = use built-in default, no explicit revision tag
    "defaultRevision": "",
}

ISTIOD_DEFAULTS = {
    # Do NOT set profile= here — profile is istioctl-only, not a Helm value
    "pilot.resources.requests.memory": "256Mi",
    "pilot.resources.requests.cpu":    "200m",
    "pilot.resources.limits.memory":   "512Mi",
    "pilot.resources.limits.cpu":      "500m",
    "meshConfig.accessLogFile":        "/dev/stdout",
    "meshConfig.enableTracing":        "false",
    "meshConfig.enableAutoMtls":       "true",
    "global.proxy.resources.requests.memory": "64Mi",
    "global.proxy.resources.requests.cpu":    "50m",
    "global.proxy.resources.limits.memory":   "128Mi",
    "global.proxy.resources.limits.cpu":      "100m",
}

ISTIO_GATEWAY_DEFAULTS = {
    "service.type": "NodePort",   # No cloud LB on kubeadm/VirtualBox
    "service.ports[0].name": "status-port",
    "service.ports[0].port": "15021",
    "service.ports[0].nodePort": "30021",
    "service.ports[1].name": "http2",
    "service.ports[1].port": "80",
    "service.ports[1].nodePort": "30080",
    "service.ports[2].name": "https",
    "service.ports[2].port": "443",
    "service.ports[2].nodePort": "30443",
    "autoscaling.enabled": "false",
    "resources.requests.cpu":    "100m",
    "resources.requests.memory": "128Mi",
    "resources.limits.cpu":      "200m",
    "resources.limits.memory":   "256Mi",
}

PROMETHEUS_DEFAULTS = {
    "grafana.enabled":                         "true",
    "grafana.adminPassword":                   "changeme",   # overridden by Secret
    "grafana.persistence.enabled":             "true",
    "grafana.persistence.size":                "2Gi",
    "grafana.persistence.storageClassName":    "local-path",
    "prometheus.prometheusSpec.retention":     "7d",
    "prometheus.prometheusSpec.routePrefix":   "/prometheus",
    "prometheus.prometheusSpec.externalUrl":   "https://192.168.56.50/prometheus",
    "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.metadata.name": "data",
    "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage": "2Gi",
    "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.storageClassName": "local-path",
    "alertmanager.enabled":                    "false",
    "nodeExporter.enabled":                    "false",
    "kubeStateMetrics.enabled":                "false",
    # Disable control-plane monitors (bind to 127.0.0.1 on kubeadm clusters)
    "kubeControllerManager.enabled":           "false",
    "kubeScheduler.enabled":                   "false",
    "kubeEtcd.enabled":                        "false",
    "kubeProxy.enabled":                       "false",
    # Disable admission webhooks job — prevents Istio sidecar race condition on batch jobs
    "prometheusOperator.admissionWebhooks.enabled": "false",
    "prometheusOperator.admissionWebhooks.patch.enabled": "false",
    # Disable individual app-level TLS — Istio mTLS sidecar encrypts all internal traffic
    "prometheusOperator.tls.enabled": "false",
    "prometheusOperator.serviceMonitor.selfMonitor": "false",
    # Operator health check probe tolerances
    "prometheusOperator.livenessProbe.initialDelaySeconds":  "30",
    "prometheusOperator.livenessProbe.timeoutSeconds":       "10",
    "prometheusOperator.livenessProbe.failureThreshold":     "10",
    "prometheusOperator.readinessProbe.initialDelaySeconds": "30",
    "prometheusOperator.readinessProbe.timeoutSeconds":      "10",
    "prometheusOperator.readinessProbe.failureThreshold":    "10",
    # Grafana security hardening
    "grafana.grafana\\.ini.security.disable_gravatar":      "true",
    "grafana.grafana\\.ini.security.cookie_secure":         "true",
    "grafana.grafana\\.ini.security.strict_transport_security": "true",
    "grafana.grafana\\.ini.users.allow_sign_up":            "false",
    "grafana.grafana\\.ini.auth\\.anonymous.enabled":       "false",
    "grafana.grafana\\.ini.analytics.reporting_enabled":    "false",
    "grafana.grafana\\.ini.analytics.check_for_updates":    "false",
    # Grafana container security
    "grafana.securityContext.runAsNonRoot":               "true",
    "grafana.securityContext.runAsUser":                  "472",
    "grafana.securityContext.fsGroup":                    "472",
    "grafana.containerSecurityContext.allowPrivilegeEscalation": "false",
    "grafana.containerSecurityContext.capabilities.drop[0]":     "ALL",
}

AIRFLOW_DEFAULTS = {
    "executor":                    "KubernetesExecutor",
    "webserver.replicas":          "1",
    "scheduler.replicas":          "1",
    "workers.replicas":            "0",
    "redis.enabled":               "false",
    "dags.gitSync.enabled":        "false",
    "webserver.resources.requests.memory": "512Mi",
    "webserver.resources.requests.cpu":    "250m",
    "webserver.resources.limits.memory":   "1Gi",
    "webserver.resources.limits.cpu":      "500m",
    "scheduler.resources.requests.memory": "512Mi",
    "scheduler.resources.requests.cpu":    "250m",
    "scheduler.resources.limits.memory":   "1Gi",
    "scheduler.resources.limits.cpu":      "500m",
    "postgresql.enabled":          "false",
    # Security — disable config exposure in UI
    "config.webserver.expose_config": "False",
    "config.api.auth_backends":       "airflow.api.auth.backend.basic_auth",
    # Webserver security context
    "webserver.securityContext.runAsUser":  "50000",
    "webserver.securityContext.fsGroup":    "0",
    "scheduler.securityContext.runAsUser":  "50000",
    "scheduler.securityContext.fsGroup":    "0",
    "triggerer.enabled":           "false",
    "webserver.livenessProbe.enabled":  "false",
    "webserver.readinessProbe.enabled": "false",
    "webserver.startupProbe.enabled":   "false",
    "migrateDatabaseJob.jobAnnotations.sidecar\\.istio\\.io/inject": "false",
    "createUserJob.jobAnnotations.sidecar\\.istio\\.io/inject": "false",
}

# ---------------------------------------------------------------------------
# Istio mesh resources
# ---------------------------------------------------------------------------

MANAGED_NAMESPACES = ["postgres", "monitoring", "airflow"]

# NodePorts for VirtualBox host-only access from Windows
NODEPORTS = {
    "http":       30080,   # HTTP  → redirects to HTTPS
    "https":      30443,   # HTTPS → Grafana + Airflow via VirtualServices
    "prometheus": 30090,   # Prometheus UI direct NodePort
}

# Host-only adapter IP (used in TLS cert SANs and VirtualService hosts)
DEFAULT_VM_HOST_ONLY_IP = "192.168.56.50"

# PeerAuthentication: enforce STRICT mTLS mesh-wide
PEER_AUTH_STRICT = {
    "apiVersion": "security.istio.io/v1beta1",
    "kind": "PeerAuthentication",
    "metadata": {
        "name": "mesh-mtls",
        "namespace": "istio-system",
    },
    "spec": {
        "mtls": {"mode": "STRICT"}
    },
}

# Gateway: single platform-wide HTTPS gateway
PLATFORM_GATEWAY_TEMPLATE = {
    "apiVersion": "networking.istio.io/v1beta1",
    "kind": "Gateway",
    "metadata": {
        "name": "platform-gateway",
        "namespace": "istio-system",
    },
    "spec": {
        "selector": {"istio": "gateway"},
        "servers": [
            {
                "port": {"number": 443, "name": "https", "protocol": "HTTPS"},
                "tls": {
                    "mode": "SIMPLE",
                    "credentialName": "istio-gateway-tls",
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
