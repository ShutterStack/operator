#!/usr/bin/env bash
# =============================================================================
# setup.sh — Kopf Platform Operator: Complete Fedora VM Setup
#
# Run this script ONCE on your Fedora VM before deploying the operator.
# Compatible with: Fedora (kubeadm cluster already running, K8s v1.36.x)
#
# What this script does:
#   1. Installs Helm 3 (FIRST — everything else depends on it)
#   2. Installs Python 3.11 + creates virtual environment
#   3. Installs Podman (for building the operator container image)
#   4. Adds all required Helm repositories
#   5. Installs cert-manager (prerequisite for TLS)
#   6. Creates all required Kubernetes namespaces
#   7. Installs local-path StorageClass (for PVC support on kubeadm)
#   8. Opens firewall ports for UI access from Windows host
#   9. Verifies Helm chart availability
#
# Usage:
#   chmod +x setup.sh
#   bash setup.sh
# =============================================================================
set -euo pipefail

YELLOW='\033[1;33m'; GREEN='\033[0;32m'; RED='\033[0;31m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERR ]${NC}  $*"; exit 1; }
section() { echo -e "\n${BOLD}${YELLOW}══════════════════════════════════════════════${NC}"; \
            echo -e "${BOLD}${YELLOW}  $*${NC}"; \
            echo -e "${BOLD}${YELLOW}══════════════════════════════════════════════${NC}"; }

# =============================================================================
# Pre-flight checks
# =============================================================================
section "Pre-flight checks"
command -v kubectl &>/dev/null || error "kubectl not found. Ensure kubeadm cluster is set up."
kubectl cluster-info &>/dev/null   || error "Cannot reach Kubernetes API. Is the cluster running?"
K8S_VERSION=$(kubectl version --output=json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['serverVersion']['gitVersion'])" 2>/dev/null || echo "unknown")
success "kubectl reachable — Server version: ${K8S_VERSION}"

VM_HOST_ONLY_IP=$(ip addr show enp0s8 2>/dev/null | grep 'inet ' | awk '{print $2}' | cut -d/ -f1 || echo "")
if [ -z "$VM_HOST_ONLY_IP" ]; then
  warn "Could not auto-detect Host-Only IP (enp0s8). Using hostname -I fallback."
  VM_HOST_ONLY_IP=$(hostname -I | awk '{print $2}')
fi
success "Host-Only IP detected: ${VM_HOST_ONLY_IP}  (Windows will use this for UI access)"

# =============================================================================
# 1. Install Helm 3  (MUST be first)
# =============================================================================
section "Step 1: Installing Helm 3"
if command -v helm &>/dev/null; then
  success "Helm already installed: $(helm version --short)"
else
  info "Downloading and installing Helm 3..."
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
  success "Helm installed: $(helm version --short)"
fi

# =============================================================================
# 2. Install Python 3 + operator virtualenv
# =============================================================================
section "Step 2: Installing Python 3 + virtual environment"
info "Ensuring Python 3 and development headers are installed..."
sudo dnf install -y python3 python3-devel python3-pip gcc --skip-unavailable -q || true

if command -v python3.11 &>/dev/null; then
  PYTHON_CMD="python3.11"
elif command -v python3 &>/dev/null; then
  PYTHON_CMD="python3"
else
  error "Python 3 is not installed."
fi
success "Using Python: $($PYTHON_CMD --version)"

VENV_DIR="$(pwd)/venv"
if [ ! -d "$VENV_DIR" ]; then
  info "Creating Python virtual environment at $VENV_DIR..."
  $PYTHON_CMD -m venv "$VENV_DIR"
fi

if [ ! -f "${VENV_DIR}/bin/pip" ]; then
  "${VENV_DIR}/bin/python" -m ensurepip --upgrade -q || true
fi

info "Installing operator Python dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip -q || true
"$VENV_DIR/bin/pip" install -r requirements.txt -q
success "Python virtual environment ready at $VENV_DIR"


# =============================================================================
# 3. Install Podman (for building the operator container image)
# =============================================================================
section "Step 3: Installing Podman"
if command -v podman &>/dev/null; then
  success "Podman already installed: $(podman --version)"
else
  info "Installing Podman..."
  sudo dnf install -y podman
  success "Podman installed: $(podman --version)"
fi

# =============================================================================
# 4. Add Helm repositories
# =============================================================================
section "Step 4: Adding Helm repositories"
declare -A REPOS=(
  ["jetstack"]="https://charts.jetstack.io"
  ["community-charts"]="https://community-charts.github.io/helm-charts"
  ["istio"]="https://istio-release.storage.googleapis.com/charts"
  ["prometheus-community"]="https://prometheus-community.github.io/helm-charts"
  ["grafana"]="https://grafana.github.io/helm-charts"
  ["apache-airflow"]="https://airflow.apache.org"
)
for name in "${!REPOS[@]}"; do
  helm repo add "$name" "${REPOS[$name]}" 2>/dev/null && info "Added repo: $name" || info "Repo already exists: $name"
done
info "Updating all Helm repositories (this may take 30s)..."
helm repo update
success "All Helm repositories ready"

# Verify critical charts exist
info "Verifying chart availability for K8s ${K8S_VERSION}..."
helm search repo istio/base              --version ">=1.24.0" | grep -q "istio/base"              && success "istio/base found"             || warn "istio/base not found — check repo"
helm search repo community-charts/postgresql --version ">=1.0.0" | grep -q "postgresql"           && success "community-charts/postgresql found" || warn "community-charts/postgresql not found"
helm search repo prometheus-community/kube-prometheus-stack --version ">=60.0.0" | grep -q "kube-prometheus-stack" && success "kube-prometheus-stack found" || warn "kube-prometheus-stack not found"
helm search repo apache-airflow/airflow  --version ">=1.14.0" | grep -q "airflow"                 && success "apache-airflow/airflow found" || warn "apache-airflow/airflow not found"

# =============================================================================
# 5. Install cert-manager (prerequisite for TLS)
# =============================================================================
section "Step 5: Installing cert-manager"
CERT_MANAGER_VERSION="v1.15.3"
if helm status cert-manager -n cert-manager &>/dev/null; then
  INSTALLED_VER=$(helm list -n cert-manager -o json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['app_version'] if d else 'unknown')" 2>/dev/null || echo "unknown")
  success "cert-manager already installed (version: $INSTALLED_VER)"
else
  info "Installing cert-manager CRDs..."
  kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.crds.yaml" --server-side

  info "Installing cert-manager ${CERT_MANAGER_VERSION} via Helm..."
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
  success "cert-manager ${CERT_MANAGER_VERSION} installed"
fi

info "Waiting for cert-manager pods to be ready..."
kubectl wait --for=condition=Available deployment/cert-manager         -n cert-manager --timeout=120s
kubectl wait --for=condition=Available deployment/cert-manager-webhook -n cert-manager --timeout=120s
success "cert-manager is fully ready ✓"

# =============================================================================
# 6. Create required namespaces
# =============================================================================
section "Step 6: Creating namespaces"
for ns in operator postgres istio-system monitoring airflow; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
  success "Namespace '$ns' ready"
done

# =============================================================================
# 7. Install local-path StorageClass (kubeadm has no default StorageClass)
# =============================================================================
section "Step 7: Setting up StorageClass"
DEFAULT_SC=$(kubectl get storageclass -o jsonpath='{.items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")].metadata.name}' 2>/dev/null || echo "")
if [ -n "$DEFAULT_SC" ]; then
  success "Default StorageClass already exists: $DEFAULT_SC"
else
  info "No default StorageClass found — installing local-path provisioner..."
  kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.28/deploy/local-path-storage.yaml
  kubectl wait --for=condition=Available deployment/local-path-provisioner \
    -n local-path-storage --timeout=60s
  kubectl patch storageclass local-path \
    -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
  success "local-path StorageClass installed and set as default"
fi

# =============================================================================
# 8. Open firewall ports for Windows host UI access
# =============================================================================
section "Step 8: Configuring firewall for Windows host access"
if command -v firewall-cmd &>/dev/null; then
  info "Opening NodePort range on firewall..."
  sudo firewall-cmd --permanent --add-port=30080/tcp   # HTTP (redirects to HTTPS)
  sudo firewall-cmd --permanent --add-port=30443/tcp   # HTTPS — Grafana + Airflow
  sudo firewall-cmd --permanent --add-port=30090/tcp   # Prometheus UI
  sudo firewall-cmd --reload
  success "Firewall ports 30080, 30443, 30090 opened"
else
  warn "firewall-cmd not found — using iptables directly"
  sudo iptables -A INPUT -p tcp --dport 30080 -j ACCEPT
  sudo iptables -A INPUT -p tcp --dport 30443 -j ACCEPT
  sudo iptables -A INPUT -p tcp --dport 30090 -j ACCEPT
  success "iptables rules added for ports 30080, 30443, 30090"
fi

# =============================================================================
# 9. Build operator container image
# =============================================================================
section "Step 9: Building the operator container image"
info "Building kopf-operator:latest with Podman..."
podman build -t kopf-operator:latest .
success "Operator image built ✓"

info "Loading image into containerd (for in-cluster use)..."
podman save kopf-operator:latest | sudo ctr -n k8s.io images import -
success "Image loaded into containerd ✓"

# =============================================================================
# Final summary
# =============================================================================
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║            SETUP COMPLETE — READY TO DEPLOY                 ║${NC}"
echo -e "${BOLD}${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${BOLD}${GREEN}║${NC}  VM Host-Only IP  : ${BOLD}${VM_HOST_ONLY_IP}${NC}"
echo -e "${BOLD}${GREEN}║${NC}  Kubernetes       : ${K8S_VERSION}"
echo -e "${BOLD}${GREEN}║${NC}  cert-manager     : ${CERT_MANAGER_VERSION} ✓"
echo -e "${BOLD}${GREEN}║${NC}  Helm             : $(helm version --short)"
echo -e "${BOLD}${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${BOLD}${GREEN}║  NEXT STEPS:                                                 ║${NC}"
echo -e "${BOLD}${GREEN}║${NC}  1. Edit platform-config.yaml                             "
echo -e "${BOLD}${GREEN}║${NC}     Make sure gatewayIpSans has: ${VM_HOST_ONLY_IP}  "
echo -e "${BOLD}${GREEN}║${NC}                                                           "
echo -e "${BOLD}${GREEN}║${NC}  2. PHASE 1 — Deploy the operator pod:                    "
echo -e "${BOLD}${GREEN}║${NC}     kubectl apply -f manifests/operator-install.yaml       "
echo -e "${BOLD}${GREEN}║${NC}     kubectl rollout status deployment/kopf-operator -n operator"
echo -e "${BOLD}${GREEN}║${NC}                                                           "
echo -e "${BOLD}${GREEN}║${NC}  3. PHASE 2 — Apply your platform config:                 "
echo -e "${BOLD}${GREEN}║${NC}     kubectl apply -f platform-config.yaml                  "
echo -e "${BOLD}${GREEN}║${NC}     kubectl get platformstack my-platform -n operator -w   "
echo -e "${BOLD}${GREEN}║${NC}                                                           "
echo -e "${BOLD}${GREEN}║${NC}  4. Access UIs from Windows browser:                      "
echo -e "${BOLD}${GREEN}║${NC}     Grafana    → https://${VM_HOST_ONLY_IP}:30443/grafana  "
echo -e "${BOLD}${GREEN}║${NC}     Airflow    → https://${VM_HOST_ONLY_IP}:30443/airflow  "
echo -e "${BOLD}${GREEN}║${NC}     Prometheus → http://${VM_HOST_ONLY_IP}:30090           "
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
