# Kopf Platform Operator Helm Chart

Kubernetes operator that manages a full platform stack (**PostgreSQL**, **Istio Service Mesh**, **Prometheus**, **Grafana**, and **Apache Airflow**) with end-to-end security, automated dependency management, health checks, and Istio-managed mTLS / TLS gateways.

---

## 🛡️ Architecture & Security Highlights

- **Single Ingress TLS Gateway**: cert-manager creates **one** TLS certificate for the Istio Gateway (`https://192.168.56.50:30443`). Individual application namespaces don't need their own certificates.
- **Mesh-wide STRICT mTLS**: All pod-to-pod traffic across `postgres`, `monitoring`, and `airflow` is encrypted automatically via Istio Envoy sidecars (`PeerAuthentication: STRICT`).
- **Security-Hardened Helm Versions**: Pinned, vulnerability-scanned chart releases compatible with Kubernetes v1.36.x:
  - **cert-manager** `v1.15.3` (fixes CVE-2024-45337)
  - **Istio** `1.24.2` (fixes CVE-2024-53269, CVE-2024-50317)
  - **PostgreSQL** `1.3.0` / App `16.4.0` (fixes CVE-2024-10978/77/76)
  - **kube-prometheus-stack** `65.2.0` / Grafana `11.3.1` (fixes CVE-2024-9264, CVE-2024-6322)
  - **Apache Airflow** `1.15.0` / App `2.9.3` (fixes CVE-2024-45034, CVE-2024-41937)
- **Container Hardening**: All pods run as non-root with dropped capabilities (`ALL`) and restricted security contexts.

---

## 🚀 Quickstart: Deploy via Helm

### Step 1: Build and Load Operator Image (Run once on VM)

```bash
cd ~/operator/Operator

# Build container image with Podman
podman build -t kopf-operator:latest .

# Import into Kubernetes containerd runtime
podman save kopf-operator:latest | sudo ctr -n k8s.io images import -
```

### Step 2: Deploy the Helm Chart

```bash
# Install the operator and trigger the full platform stack deployment
helm install platform-operator ./chart -n operator --create-namespace
```

> **Tip**: You can customize settings via `--set` or by editing [`chart/values.yaml`](./chart/values.yaml):
> ```bash
> helm install platform-operator ./chart -n operator --create-namespace \
>   --set platform.network.hostOnlyIp="192.168.56.50" \
>   --set platform.components.postgres.database="customdb"
> ```

---

## 📦 What the Helm Chart Does Automatically

When you run `helm install platform-operator ./chart -n operator`, the chart:
1. Installs all Custom Resource Definitions (`crds/`).
2. Creates the Operator `ServiceAccount`, `ClusterRole`, `ClusterRoleBinding`, and `Deployment`.
3. Creates the `PlatformStack` Custom Resource configured from `values.yaml`.
4. Creates the `prometheus-nodeport` Service.
5. The Operator pod immediately detects the `PlatformStack` and deploys in dependency order:
   - **cert-manager** & Self-signed CA ClusterIssuer
   - **Istio Base, Istiod, & Ingress Gateway** (NodePorts 30080/30443)
   - Labels namespaces (`postgres`, `monitoring`, `airflow`) for Istio sidecar injection
   - Mesh-wide **STRICT mTLS** (`PeerAuthentication`) & `DestinationRules`
   - Ingress Gateway TLS Certificate & VirtualServices (`/grafana`, `/airflow`)
   - **PostgreSQL 16.4** (with auto-generated credentials Secret)
   - **Prometheus 2.55 & Grafana 11.3.1** (with 5 pre-loaded dashboards)
   - **Apache Airflow 2.9.3** (KubernetesExecutor connected to PostgreSQL)

---

## 🖥️ Accessing UIs from Windows Host

Open these URLs in your Windows browser:

| Application | URL | Default Credentials |
|---|---|---|
| 📊 **Grafana** | `https://192.168.56.50:30443/grafana` | User: `admin` / Password: *(see command below)* |
| 🌀 **Airflow** | `https://192.168.56.50:30443/airflow` | User: `admin` / Password: `admin` *(change on 1st login)* |
| 🔥 **Prometheus** | `http://192.168.56.50:30090` | Direct access (no login) |

> **Browser Warning**: When visiting `https://192.168.56.50:30443`, click **Advanced → Proceed to 192.168.56.50 (unsafe)** to accept the self-signed TLS cert.

### Retrieve Auto-Generated Credentials

```bash
# Get Grafana admin password
kubectl get secret grafana-admin-credentials -n monitoring \
  -o jsonpath='{.data.admin-password}' | base64 -d && echo

# Get PostgreSQL connection string
kubectl get secret postgres-credentials -n postgres \
  -o jsonpath='{.data.connection-string}' | base64 -d && echo
```

---

## 🔍 Monitoring & Verifying Health

```bash
# Watch overall deployment status
kubectl get platformstack my-platform -n operator -w

# Check all platform pods
kubectl get pods -A | grep -E "operator|postgres|istio-system|monitoring|airflow"

# Check Istio mTLS and Gateway
kubectl get peerauthentication -n istio-system
kubectl get gateway,virtualservice -n istio-system
kubectl get certificate -n istio-system

# Operator logs
kubectl logs -n operator deploy/platform-operator-platform-operator -f
```

---

## 🧹 Cleanup & Teardown

To cleanly uninstall the entire stack:

```bash
# Uninstalls the operator, CRs, and triggers reverse-order teardown of all components
helm uninstall platform-operator -n operator
```
