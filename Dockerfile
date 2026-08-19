FROM python:3.11-slim

LABEL maintainer="platform-ops"
LABEL description="Kopf Operator — Helm-based platform stack manager"

# ── System dependencies ──────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

# ── Install Helm 3 ──────────────────────────────────────────────────────────
ARG HELM_VERSION=v3.15.4
RUN curl -fsSL https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz \
    | tar -zx --strip-components=1 -C /usr/local/bin linux-amd64/helm \
    && helm version --short

# ── Install kubectl ─────────────────────────────────────────────────────────
ARG KUBECTL_VERSION=v1.30.3
RUN curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client

# ── Python dependencies ──────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Operator source ──────────────────────────────────────────────────────────
COPY platform_operator/ ./platform_operator/
COPY helm-values/ ./helm-values/

# ── Helm home dirs pinned to /app/.helm so they survive the USER switch ──────
# These must be set BEFORE helm repo add so repos are written to /app/.helm,
# which will be chowned to appuser in the next step.
ENV HELM_DATA_HOME=/app/.helm/data
ENV HELM_CONFIG_HOME=/app/.helm/config
ENV HELM_CACHE_HOME=/app/.helm/cache

RUN helm repo add jetstack             https://charts.jetstack.io \
    && helm repo add istio              https://istio-release.storage.googleapis.com/charts \
    && helm repo add prometheus-community https://prometheus-community.github.io/helm-charts \
    && helm repo add grafana            https://grafana.github.io/helm-charts \
    && helm repo add apache-airflow     https://airflow.apache.org \
    && helm repo update



# ── Non-root user ────────────────────────────────────────────────────────────
# /app (including /app/.helm with all repos) is chowned to appuser so Helm
# can read repos and write cache at runtime without needing root.
RUN groupadd -g 1000 appuser \
    && useradd -u 1000 -g appuser -m -d /home/appuser -s /bin/bash appuser \
    && chown -R appuser:appuser /app /home/appuser

USER appuser
ENV HOME=/home/appuser
ENV PYTHONPATH=/app

# ── Healthcheck (kopf readiness probe) ───────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1

# kopf is the entrypoint; CMD provides default args that k8s deployment.yaml overrides
ENTRYPOINT ["kopf"]
CMD ["run", "/app/platform_operator/main.py", "--all-namespaces", "--liveness=http://0.0.0.0:8080/healthz"]
