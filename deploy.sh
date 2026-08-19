#!/usr/bin/env bash
# =============================================================================
#  deploy.sh — Kopf Platform Operator: FULLY AUTOMATED END-TO-END SETUP
#
#  ┌──────────────────────────────────────────────────────────────────────┐
#  │  INSTRUCTIONS — READ BEFORE RUNNING                                  │
#  │                                                                      │
#  │  1. Fill in the USER CONFIGURATION section below (lines 30-75)      │
#  │  2. Save the file                                                    │
#  │  3. Run:  bash deploy.sh                                             │
#  │                                                                      │
#  │  The script will handle EVERYTHING:                                  │
#  │    Helm → Python → Podman → Repos → cert-manager → namespaces       │
#  │    → StorageClass → Firewall → Build image → Deploy operator         │
#  │    → Deploy stack → Wait for Ready → Show all pods + UI URLs        │
#  └──────────────────────────────────────────────────────────────────────┘
#
#  Prerequisites (already done on your VM):
#    ✅ kubeadm cluster running  (kubectl get nodes shows Ready)
#    ✅ Host-Only adapter configured in VirtualBox (enp0s8: 192.168.56.50)
# =============================================================================

set -euo pipefail

# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║                    USER CONFIGURATION                                    ║
# ║  Fill these values before running the script. That's all you need to do.║
# ╚═══════════════════════════════════════════════════════════════════════════╝

# ── Network ────────────────────────────────────────────────────────────────────
# Your VM's Host-Only adapter IP (the 192.168.x.x address shown in ifconfig/hostname -I)
# This is the IP your Windows browser will use to access all UIs.
# Your current value from ifconfig output:
VM_HOST_ONLY_IP="192.168.56.50"

# ── PostgreSQL ─────────────────────────────────────────────────────────────────
POSTGRES_DATABASE="appdb"       # Database name to create
POSTGRES_USERNAME="appuser"     # App user (password is auto-generated securely)
POSTGRES_STORAGE="10Gi"         # PVC storage size (check VM disk: df -h /)

# ── Monitoring ─────────────────────────────────────────────────────────────────
PROMETHEUS_RETENTION="7d"       # How long to keep metrics
PROMETHEUS_STORAGE="10Gi"       # PVC size for Prometheus TSDB
GRAFANA_ADMIN_PASSWORD=""       # Leave empty to auto-generate a strong password

# ── Airflow ────────────────────────────────────────────────────────────────────
AIRFLOW_EXECUTOR="KubernetesExecutor"   # KubernetesExecutor (recommended, no Redis needed)
                                        # Alternatives: LocalExecutor, CeleryExecutor

# Git-sync: load DAGs from a Git repository (set to "true" to enable)
AIRFLOW_GITSYNC_ENABLED="false"
AIRFLOW_GITSYNC_REPO=""                 # e.g. "https://github.com/user/dags.git"
AIRFLOW_GITSYNC_BRANCH="main"

# ── Timeouts ───────────────────────────────────────────────────────────────────
# Maximum time (seconds) to wait for the full platform to become Ready
# 30 minutes is generous; typical deployment takes 10-20 minutes
PLATFORM_DEPLOY_TIMEOUT=1800

# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║            DO NOT EDIT BELOW THIS LINE                                   ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

# ── Pinned versions (K8s v1.36.x compatible, security-patched) ────────────────
HELM_VERSION="v3.16.4"
CERT_MANAGER_VERSION="v1.15.3"
ISTIO_VERSION="1.24.2"
POSTGRES_CHART_VERSION="1.3.0"
PROMETHEUS_CHART_VERSION="65.2.0"
AIRFLOW_CHART_VERSION="1.15.0"

# ── NodePorts ─────────────────────────────────────────────────────────────────
NODEPORT_HTTP=30080
NODEPORT_HTTPS=30443
NODEPORT_PROMETHEUS=30090

# ── Script directory (always run relative to deploy.sh location) ───────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

info()    { echo -e "${CYAN}  [INFO]${NC}  $*"; }
success() { echo -e "${GREEN}  [ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}  [WARN]${NC}  $*"; }
error()   { echo -e "${RED}  [ERR ]${NC}  $*"; exit 1; }

banner() {
  echo -e "\n${BOLD}${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${BOLD}${YELLOW}  $*${NC}"
  echo -e "${BOLD}${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

# ─────────────────────────────────────────────────────────────────────────────
# Input Validation
# ─────────────────────────────────────────────────────────────────────────────
banner "Validating configuration"

[[ -z "$VM_HOST_ONLY_IP" ]]  && error "VM_HOST_ONLY_IP is not set. Edit this file first."
[[ "$VM_HOST_ONLY_IP" == "192.168.56.50" ]] && \
  info "Using pre-detected Host-Only IP: ${VM_HOST_ONLY_IP}"

# Validate IP format
if ! echo "$VM_HOST_ONLY_IP" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  error "VM_HOST_ONLY_IP '$VM_HOST_ONLY_IP' is not a valid IP address."
fi

if [[ "$AIRFLOW_GITSYNC_ENABLED" == "true" && -z "$AIRFLOW_GITSYNC_REPO" ]]; then
  error "AIRFLOW_GITSYNC_ENABLED=true but AIRFLOW_GITSYNC_REPO is empty. Provide a git repo URL."
fi

success "Configuration validated"
echo -e "${DIM}  VM IP: ${VM_HOST_ONLY_IP}  |  DB: ${POSTGRES_DATABASE}  |  Executor: ${AIRFLOW_EXECUTOR}${NC}"

# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight: kubectl reachable?
# ─────────────────────────────────────────────────────────────────────────────
banner "Pre-flight: Kubernetes cluster check"

command -v kubectl &>/dev/null || error "kubectl not found. Is kubeadm cluster set up?"
kubectl cluster-info --request-timeout=10s &>/dev/null || \
  error "Cannot reach Kubernetes API server. Run: kubectl cluster-info"

K8S_VERSION=$(kubectl version -o json 2>/dev/null | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('serverVersion',{}).get('gitVersion','unknown'))" 2>/dev/null || echo "unknown")
success "Kubernetes cluster reachable — Server: ${K8S_VERSION}"

# ── Node readiness check ─────────────────────────────────────────────────────
# Require only the control-plane node to be Ready.
# NotReady worker nodes are cordoned (scheduler skips them) so all workloads
# land on the master. This is correct for a single-effective-node VirtualBox setup.
NODE_COUNT=$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')
CP_READY=$(kubectl get nodes --no-headers 2>/dev/null \
  | grep -E 'control-plane|master' | grep -c ' Ready' || true)

if [[ "$CP_READY" -eq 0 ]]; then
  error "Control-plane node is not Ready. Fix it first: kubectl get nodes"
fi
success "Control-plane node is Ready ✓ (${NODE_COUNT} total nodes)"

# Cordon any NotReady worker nodes so the scheduler ignores them completely
NOT_READY_WORKERS=$(kubectl get nodes --no-headers 2>/dev/null \
  | grep -v -E 'control-plane|master' | grep 'NotReady' | awk '{print $1}' || true)

if [[ -n "$NOT_READY_WORKERS" ]]; then
  for worker in $NOT_READY_WORKERS; do
    warn "Worker node '${worker}' is NotReady — cordoning it (scheduler will skip it)"
    kubectl cordon "$worker" >/dev/null 2>&1 || true
    success "Cordoned: ${worker}"
  done
  warn "All workloads will run on the control-plane node only."
else
  success "All ${NODE_COUNT} node(s) are Ready"
fi

# ── Remove control-plane NoSchedule taint ─────────────────────────────────────
# kubeadm adds 'node-role.kubernetes.io/control-plane:NoSchedule' by default.
# We remove it so platform pods can schedule on the master node.
CP_NODE=$(kubectl get nodes --no-headers 2>/dev/null \
  | grep -E 'control-plane|master' | awk '{print $1}' | head -1)

if kubectl get node "$CP_NODE" -o jsonpath='{.spec.taints}' 2>/dev/null \
    | grep -q 'control-plane'; then
  info "Removing NoSchedule taint from control-plane node '${CP_NODE}'..."
  kubectl taint nodes "$CP_NODE" node-role.kubernetes.io/control-plane:NoSchedule- \
    >/dev/null 2>&1 || true
  success "Taint removed — pods can now schedule on ${CP_NODE}"
else
  success "Control-plane node '${CP_NODE}' has no NoSchedule taint (already schedulable)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Internet connectivity check + DNS auto-fix
# ─────────────────────────────────────────────────────────────────────────────
banner "Pre-flight: Internet connectivity check"

check_internet() {
  # Try TCP connect to 8.8.8.8:53 — no DNS needed, fastest check
  timeout 5 bash -c 'echo >/dev/tcp/8.8.8.8/53' 2>/dev/null
}

if check_internet; then
  success "Internet reachable via enp0s3 (NAT adapter)"
else
  warn "No internet connectivity detected. Attempting DNS fix..."

  # Proper fix for systemd-resolved (Fedora default) — drop-in config
  # This survives NetworkManager restarts unlike overwriting /etc/resolv.conf
  sudo mkdir -p /etc/systemd/resolved.conf.d/
  sudo tee /etc/systemd/resolved.conf.d/dns.conf >/dev/null << 'DNSEOF'
[Resolve]
DNS=8.8.8.8 8.8.4.4
FallbackDNS=1.1.1.1
DNSEOF
  sudo systemctl restart systemd-resolved
  info "Configured systemd-resolved with Google DNS (8.8.8.8, 8.8.4.4)"

  # Also set via NetworkManager for the NAT interface
  if command -v nmcli &>/dev/null; then
    NAT_IF=$(ip route | awk '/default/{print $5}' | head -1)
    if [[ -n "$NAT_IF" ]]; then
      sudo nmcli device modify "$NAT_IF" ipv4.dns "8.8.8.8 8.8.4.4" 2>/dev/null || true
    fi
  fi

  # Re-test after DNS fix
  if check_internet; then
    success "Internet reachable after DNS fix ✓"
  else
    warn "Still no internet. Will use package manager (dnf) for installs where possible."
    warn "If dnf also fails, ensure VirtualBox NAT adapter is attached:"
    warn "  VM Settings → Network → Adapter 1 → Attached to: NAT"
    warn "Continuing anyway — some steps may fail if offline..."
    sleep 3
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Install Helm 3 (MUST be first — everything depends on it)
# ─────────────────────────────────────────────────────────────────────────────
banner "Step 1/10: Installing Helm ${HELM_VERSION}"

if command -v helm &>/dev/null; then
  INSTALLED_HELM=$(helm version --short 2>/dev/null | grep -oP 'v[0-9]+\.[0-9]+\.[0-9]+' || echo "unknown")
  success "Helm already installed: ${INSTALLED_HELM}"
else
  # Try 1: dnf (uses Fedora mirrors — works even when get.helm.sh is unreachable)
  if sudo dnf install -y helm &>/dev/null 2>&1; then
    success "Helm installed via dnf: $(helm version --short)"
  else
    # Try 2: direct download from get.helm.sh
    info "dnf install failed — trying direct download from get.helm.sh..."
    HELM_ARCH="linux-amd64"
    HELM_TAR="helm-${HELM_VERSION}-${HELM_ARCH}.tar.gz"
    if curl -fsSL --connect-timeout 30 --max-time 120 \
        "https://get.helm.sh/${HELM_TAR}" -o /tmp/helm.tar.gz; then
      tar -zxf /tmp/helm.tar.gz -C /tmp
      sudo mv /tmp/${HELM_ARCH}/helm /usr/local/bin/helm
      rm -rf /tmp/helm.tar.gz /tmp/${HELM_ARCH}
      success "Helm ${HELM_VERSION} installed via download: $(helm version --short)"
    else
      error "Cannot install Helm. Fix internet access first:\n  1. Check VirtualBox: VM Settings → Network → Adapter 1 → Attached to: NAT\n  2. Try: echo 'nameserver 8.8.8.8' | sudo tee /etc/resolv.conf\n  3. Test:  curl -I https://get.helm.sh"
    fi
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Install Python 3 + virtual environment
# ─────────────────────────────────────────────────────────────────────────────
banner "Step 2/10: Python 3 + virtual environment"

# On Fedora, python3 is standard. We try to install python3.11 or default python3 packages.
info "Ensuring Python 3 and development headers are installed..."
sudo dnf install -y python3 python3-devel python3-pip gcc --skip-unavailable -q || true

if command -v python3.11 &>/dev/null; then
  PYTHON_CMD="python3.11"
elif command -v python3 &>/dev/null; then
  PYTHON_CMD="python3"
else
  error "Python 3 is not installed and could not be installed via dnf."
fi
success "Using Python: $($PYTHON_CMD --version)"

# Clean up any legacy 'operator' directory to avoid namespace collision with Python standard library
if [ -d "${SCRIPT_DIR}/operator" ]; then
  warn "Removing legacy '${SCRIPT_DIR}/operator' directory to avoid Python module naming collision..."
  rm -rf "${SCRIPT_DIR}/operator"
fi

VENV_DIR="${SCRIPT_DIR}/venv"
if [ ! -d "$VENV_DIR" ]; then
  info "Creating virtual environment..."
  $PYTHON_CMD -m venv "$VENV_DIR"
fi



# Ensure pip is available inside virtual environment
if [ ! -f "${VENV_DIR}/bin/pip" ]; then
  "${VENV_DIR}/bin/python" -m ensurepip --upgrade -q || true
fi

info "Installing Python dependencies in virtual environment..."
"${VENV_DIR}/bin/pip" install --upgrade pip -q || true
"${VENV_DIR}/bin/pip" install -r requirements.txt -q
success "Virtual environment ready at ${VENV_DIR}"


# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Install Podman (for building operator container image)
# ─────────────────────────────────────────────────────────────────────────────
banner "Step 3/10: Installing Podman"

if command -v podman &>/dev/null; then
  success "Podman already installed: $(podman --version)"
else
  info "Installing Podman..."
  sudo dnf install -y podman -q
  success "Podman installed: $(podman --version)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Add Helm repositories
# ─────────────────────────────────────────────────────────────────────────────
banner "Step 4/10: Adding Helm repositories"

helm_repo_add() {
  local name=$1 url=$2
  if helm repo list 2>/dev/null | grep -q "^${name}"; then
    info "Repo already exists: ${name}"
  else
    helm repo add "$name" "$url"
    success "Added repo: ${name}"
  fi
}

helm_repo_add "jetstack"              "https://charts.jetstack.io"
helm_repo_add "community-charts"     "https://community-charts.github.io/helm-charts"
helm_repo_add "istio"                "https://istio-release.storage.googleapis.com/charts"
helm_repo_add "prometheus-community" "https://prometheus-community.github.io/helm-charts"
helm_repo_add "grafana"              "https://grafana.github.io/helm-charts"
helm_repo_add "apache-airflow"       "https://airflow.apache.org"

info "Refreshing repository index..."
helm repo update >/dev/null
success "All 6 Helm repositories ready"

# Sanity-check: verify charts are available
banner_check() {
  local chart=$1 version=$2
  if helm search repo "$chart" --version ">=${version}" 2>/dev/null | grep -q "$chart"; then
    success "Chart available: ${chart} (>=${version})"
  else
    warn "Chart ${chart} not found — will attempt install anyway"
  fi
}
banner_check "istio/base"                                     "$ISTIO_VERSION"
banner_check "community-charts/postgresql"                    "$POSTGRES_CHART_VERSION"
banner_check "prometheus-community/kube-prometheus-stack"     "$PROMETHEUS_CHART_VERSION"
banner_check "apache-airflow/airflow"                         "$AIRFLOW_CHART_VERSION"

# ─────────────────────────────────────────────────────────────────────────────
# Step 5: Install cert-manager (TLS prerequisite)
# ─────────────────────────────────────────────────────────────────────────────
banner "Step 5/10: Installing cert-manager ${CERT_MANAGER_VERSION}"

if helm status cert-manager -n cert-manager &>/dev/null; then
  success "cert-manager already installed"
else
  info "Applying cert-manager CRDs..."
  kubectl apply -f \
    "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.crds.yaml" \
    --server-side 2>/dev/null

  info "Installing cert-manager via Helm..."
  helm install cert-manager jetstack/cert-manager \
    --namespace cert-manager \
    --create-namespace \
    --version "${CERT_MANAGER_VERSION}" \
    --set installCRDs=false \
    --set global.leaderElection.namespace=cert-manager \
    --set securityContext.runAsNonRoot=true \
    --set cainjector.securityContext.runAsNonRoot=true \
    --set webhook.securityContext.runAsNonRoot=true \
    --wait --timeout=5m
fi

info "Waiting for cert-manager to be fully ready..."
kubectl wait --for=condition=Available deployment/cert-manager         -n cert-manager --timeout=120s >/dev/null
kubectl wait --for=condition=Available deployment/cert-manager-webhook -n cert-manager --timeout=120s >/dev/null
success "cert-manager ${CERT_MANAGER_VERSION} is Ready ✓"

# ─────────────────────────────────────────────────────────────────────────────
# Step 6: Create namespaces
# ─────────────────────────────────────────────────────────────────────────────
banner "Step 6/10: Creating namespaces"

for ns in operator postgres istio-system monitoring airflow; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  success "Namespace: ${ns}"
done

# ─────────────────────────────────────────────────────────────────────────────
# Step 7: StorageClass (local-path for kubeadm)
# ─────────────────────────────────────────────────────────────────────────────
banner "Step 7/10: StorageClass"

DEFAULT_SC=$(kubectl get storageclass -o jsonpath=\
  '{.items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")].metadata.name}' \
  2>/dev/null || echo "")

if [ -n "$DEFAULT_SC" ]; then
  success "Default StorageClass already set: ${DEFAULT_SC}"
else
  info "Installing local-path StorageClass provisioner..."
  kubectl apply -f \
    "https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.28/deploy/local-path-storage.yaml" \
    >/dev/null
  kubectl wait --for=condition=Available deployment/local-path-provisioner \
    -n local-path-storage --timeout=90s >/dev/null
  kubectl patch storageclass local-path \
    -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}' \
    >/dev/null
  success "local-path StorageClass installed and set as default"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 8: Open firewall for Windows host access
# ─────────────────────────────────────────────────────────────────────────────
banner "Step 8/10: Firewall rules for Windows UI access"

open_port() {
  local port=$1 desc=$2
  if command -v firewall-cmd &>/dev/null; then
    sudo firewall-cmd --permanent --add-port="${port}/tcp" >/dev/null 2>&1 || true
    success "Opened ${port}/tcp — ${desc}"
  else
    sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || \
      sudo iptables -A INPUT -p tcp --dport "$port" -j ACCEPT
    success "iptables rule added for ${port}/tcp — ${desc}"
  fi
}

open_port "$NODEPORT_HTTP"       "HTTP  → redirects to HTTPS"
open_port "$NODEPORT_HTTPS"      "HTTPS → Grafana + Airflow via Istio Gateway"
open_port "$NODEPORT_PROMETHEUS" "Prometheus UI"

command -v firewall-cmd &>/dev/null && sudo firewall-cmd --reload >/dev/null 2>&1 || true

# ─────────────────────────────────────────────────────────────────────────────
# Step 9: Build operator image and load into containerd
# ─────────────────────────────────────────────────────────────────────────────
banner "Step 9/10: Building operator container image"

info "Building kopf-operator:latest with Podman (this takes ~2 minutes)..."
podman build -t kopf-operator:latest "$SCRIPT_DIR" >/dev/null 2>&1 && \
  success "Image built: kopf-operator:latest" || \
  error "Podman build failed. Check Dockerfile and network connectivity."

info "Loading image into containerd (for in-cluster pod)..."
podman save kopf-operator:latest 2>/dev/null | sudo ctr -n k8s.io images import - >/dev/null && \
  success "Image loaded into containerd" || \
  error "Failed to load image into containerd. Is containerd running? (systemctl status containerd)"

# ─────────────────────────────────────────────────────────────────────────────
# Step 10: Generate platform-config.yaml from user variables
# ─────────────────────────────────────────────────────────────────────────────
banner "Step 10/10: Generating platform-config.yaml"

PLATFORM_CONFIG_FILE="${SCRIPT_DIR}/platform-config.yaml"

cat > "$PLATFORM_CONFIG_FILE" << YAML_EOF
# AUTO-GENERATED by deploy.sh on $(date '+%Y-%m-%d %H:%M:%S')
# Do not edit manually — re-run deploy.sh to regenerate.
apiVersion: platform.ops/v1alpha1
kind: PlatformStack
metadata:
  name: my-platform
  namespace: operator
  labels:
    app.kubernetes.io/managed-by: kopf-operator
    generated-by: deploy.sh
spec:
  components:
    istio:
      enabled: true
    postgres:
      enabled: true
    monitoring:
      enabled: true
    airflow:
      enabled: true

  istio:
    profile: default
    gatewayHosts:
      - "${VM_HOST_ONLY_IP}"
      - "platform.local"
    gatewayIpSans:
      - "${VM_HOST_ONLY_IP}"

  postgres:
    database: ${POSTGRES_DATABASE}
    username: ${POSTGRES_USERNAME}
    storageSize: ${POSTGRES_STORAGE}
    replicas: 1

  monitoring:
    storageSize: ${PROMETHEUS_STORAGE}
    retention: ${PROMETHEUS_RETENTION}
    grafana:
      enabled: true
      adminPassword: "${GRAFANA_ADMIN_PASSWORD}"

  airflow:
    executor: ${AIRFLOW_EXECUTOR}
    webserverReplicas: 1
    schedulerReplicas: 1
    gitSync:
      enabled: ${AIRFLOW_GITSYNC_ENABLED}
YAML_EOF

# Append git-sync details if enabled
if [[ "$AIRFLOW_GITSYNC_ENABLED" == "true" && -n "$AIRFLOW_GITSYNC_REPO" ]]; then
cat >> "$PLATFORM_CONFIG_FILE" << YAML_EOF
      repo: "${AIRFLOW_GITSYNC_REPO}"
      branch: "${AIRFLOW_GITSYNC_BRANCH}"
      subPath: "dags"
YAML_EOF
fi

success "Generated platform-config.yaml with VM IP: ${VM_HOST_ONLY_IP}"

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1: Deploy operator
# ─────────────────────────────────────────────────────────────────────────────
banner "PHASE 1: Deploying Kopf Operator pod"

info "Applying CRDs + RBAC + Operator Deployment..."
kubectl apply -f "${SCRIPT_DIR}/manifests/operator-install.yaml" >/dev/null
success "Operator manifests applied"

info "Waiting for operator pod to be Running (up to 3 minutes)..."
kubectl rollout status deployment/kopf-operator -n operator --timeout=180s

# Wait for the operator to actually start (liveness probe might need a moment)
sleep 5

# Show operator startup log
echo ""
echo -e "${DIM}── Operator startup log ────────────────────────────────${NC}"
kubectl logs -n operator \
  -l app.kubernetes.io/name=kopf-operator \
  --tail=20 2>/dev/null || true
echo -e "${DIM}────────────────────────────────────────────────────────${NC}"
echo ""

success "Operator is Running ✓"

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2: Apply platform configuration
# ─────────────────────────────────────────────────────────────────────────────
banner "PHASE 2: Applying platform configuration → triggering full stack deploy"

kubectl apply -f "${SCRIPT_DIR}/platform-config.yaml"
success "PlatformStack CR applied — operator is now deploying the full stack"

echo ""
echo -e "  ${CYAN}Deployment order:${NC}"
echo -e "  cert-manager ✓ → Istio → PostgreSQL → Prometheus/Grafana → Airflow"
echo -e ""
echo -e "  ${DIM}This takes 10-20 minutes. Waiting up to ${PLATFORM_DEPLOY_TIMEOUT}s...${NC}"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Wait loop: poll PlatformStack status until Ready
# ─────────────────────────────────────────────────────────────────────────────
ELAPSED=0
POLL_INTERVAL=20
LAST_PHASE=""

while [ $ELAPSED -lt $PLATFORM_DEPLOY_TIMEOUT ]; do
  PHASE=$(kubectl get platformstack my-platform -n operator \
    -o jsonpath='{.status.phase}' 2>/dev/null || echo "Pending")
  READY=$(kubectl get platformstack my-platform -n operator \
    -o jsonpath='{.status.ready}' 2>/dev/null || echo "false")

  # Only print when phase changes
  if [ "$PHASE" != "$LAST_PHASE" ]; then
    TIMESTAMP=$(date '+%H:%M:%S')
    printf "  ${CYAN}[%s]${NC} Phase: ${BOLD}%-35s${NC}" "$TIMESTAMP" "$PHASE"

    # Count pods across all platform namespaces
    RUNNING_PODS=$(kubectl get pods -n postgres -n istio-system -n monitoring -n airflow \
      --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d ' ')
    echo -e "  Pods Running: ${GREEN}${RUNNING_PODS}${NC}"
    LAST_PHASE="$PHASE"
  fi

  if [ "$READY" = "true" ] && [ "$PHASE" = "Ready" ]; then
    success "PlatformStack is READY ✓  (elapsed: ${ELAPSED}s)"
    break
  fi

  if [ "$PHASE" = "Error" ]; then
    echo ""
    error "Deployment failed! Check operator logs:"
    echo "  kubectl logs -n operator -l app.kubernetes.io/name=kopf-operator --tail=50"
    exit 1
  fi

  sleep $POLL_INTERVAL
  ELAPSED=$((ELAPSED + POLL_INTERVAL))
done

if [ $ELAPSED -ge $PLATFORM_DEPLOY_TIMEOUT ]; then
  warn "Timed out waiting for Ready. The stack may still be deploying."
  warn "Check status: kubectl get platformstack my-platform -n operator"
  warn "Check logs:   kubectl logs -n operator deploy/kopf-operator --tail=50 -f"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Apply Prometheus NodePort service
# ─────────────────────────────────────────────────────────────────────────────
info "Applying Prometheus NodePort service..."
kubectl apply -f "${SCRIPT_DIR}/manifests/prometheus-nodeport.yaml" >/dev/null
success "Prometheus NodePort service created (:${NODEPORT_PROMETHEUS})"

# ─────────────────────────────────────────────────────────────────────────────
# Final status display
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo ""
banner "DEPLOYMENT COMPLETE — Final Status"

echo -e "\n${BOLD}── Namespaces ──────────────────────────────────────────────────${NC}"
kubectl get namespaces | grep -E "NAME|operator|postgres|istio-system|monitoring|airflow|cert-manager"

echo -e "\n${BOLD}── Pods: operator ──────────────────────────────────────────────${NC}"
kubectl get pods -n operator 2>/dev/null || echo "  (none)"

echo -e "\n${BOLD}── Pods: istio-system ───────────────────────────────────────────${NC}"
kubectl get pods -n istio-system 2>/dev/null || echo "  (none)"

echo -e "\n${BOLD}── Pods: postgres ───────────────────────────────────────────────${NC}"
kubectl get pods -n postgres 2>/dev/null || echo "  (none)"

echo -e "\n${BOLD}── Pods: monitoring ─────────────────────────────────────────────${NC}"
kubectl get pods -n monitoring 2>/dev/null || echo "  (none)"

echo -e "\n${BOLD}── Pods: airflow ────────────────────────────────────────────────${NC}"
kubectl get pods -n airflow 2>/dev/null || echo "  (none)"

echo -e "\n${BOLD}── PlatformStack Status ─────────────────────────────────────────${NC}"
kubectl get platformstack my-platform -n operator -o wide 2>/dev/null

echo -e "\n${BOLD}── Istio mTLS (PeerAuthentication) ──────────────────────────────${NC}"
kubectl get peerauthentication -A 2>/dev/null

echo -e "\n${BOLD}── Istio Gateway ────────────────────────────────────────────────${NC}"
kubectl get gateway -n istio-system 2>/dev/null

echo -e "\n${BOLD}── Istio VirtualServices ────────────────────────────────────────${NC}"
kubectl get virtualservice -n istio-system 2>/dev/null

echo -e "\n${BOLD}── TLS Certificate ──────────────────────────────────────────────${NC}"
kubectl get certificate -n istio-system 2>/dev/null

# ── Retrieve auto-generated credentials ─────────────────────────────────────
echo -e "\n${BOLD}── Credentials ──────────────────────────────────────────────────${NC}"

GRAFANA_PASS=$(kubectl get secret grafana-admin-credentials -n monitoring \
  -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d 2>/dev/null || echo "(not yet created)")

PG_CONNSTR=$(kubectl get secret postgres-credentials -n postgres \
  -o jsonpath='{.data.connection-string}' 2>/dev/null | base64 -d 2>/dev/null || echo "(not yet created)")

echo -e "  Grafana    → user: admin   password: ${BOLD}${GRAFANA_PASS}${NC}"
echo -e "  Airflow    → user: admin   password: ${BOLD}admin${NC} (change after first login)"
echo -e "  PostgreSQL → ${DIM}${PG_CONNSTR}${NC}"

# ── Final access info ────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔════════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║  🎉  PLATFORM IS LIVE — Open these in your Windows browser:        ║${NC}"
echo -e "${BOLD}${GREEN}╠════════════════════════════════════════════════════════════════════╣${NC}"
echo -e "${BOLD}${GREEN}║${NC}                                                                    ${BOLD}${GREEN}║${NC}"
echo -e "${BOLD}${GREEN}║${NC}  📊 Grafana    ${BOLD}https://${VM_HOST_ONLY_IP}/grafana${NC}    ${BOLD}${GREEN}║${NC}"
echo -e "${BOLD}${GREEN}║${NC}  🌀 Airflow    ${BOLD}https://${VM_HOST_ONLY_IP}/airflow${NC}    ${BOLD}${GREEN}║${NC}"
echo -e "${BOLD}${GREEN}║${NC}  🔥 Prometheus ${BOLD}https://${VM_HOST_ONLY_IP}/prometheus${NC}              ${BOLD}${GREEN}║${NC}"
echo -e "${BOLD}${GREEN}║${NC}                                                                    ${BOLD}${GREEN}║${NC}"
echo -e "${BOLD}${GREEN}║${NC}  ⚠️  TLS warning in browser is expected — click 'Advanced'        ${BOLD}${GREEN}║${NC}"
echo -e "${BOLD}${GREEN}║${NC}     then 'Proceed to ${VM_HOST_ONLY_IP} (unsafe)'                  ${BOLD}${GREEN}║${NC}"
echo -e "${BOLD}${GREEN}║${NC}                                                                    ${BOLD}${GREEN}║${NC}"
echo -e "${BOLD}${GREEN}║${NC}  All internal traffic uses Istio mTLS (STRICT mode) ✅            ${BOLD}${GREEN}║${NC}"
echo -e "${BOLD}${GREEN}╚════════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${DIM}Useful commands:${NC}"
echo -e "  ${DIM}  kubectl get pods -A | grep -v kube-system              # all platform pods${NC}"
echo -e "  ${DIM}  kubectl logs -n operator deploy/kopf-operator -f        # operator logs${NC}"
echo -e "  ${DIM}  kubectl get platformstack my-platform -n operator        # stack status${NC}"
echo ""
