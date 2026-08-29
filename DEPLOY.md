# Calendar Backend Deployment

GitOps deployment for the Calendar backend on the Civo `pods-cluster` using **GitHub Actions + Docker Hub + Argo CD**.

## Architecture

```text
develop push
  -> GitHub Actions CI (test, build, validate)
  -> push image to Docker Hub as <username>/calendar-backend:<git-sha>
  -> kubectl rollout with SHA tag (no Git manifest commits)
  -> HTTPS health verification and application URL summary
```

Argo CD owns cluster deployment. GitHub Actions does **not** run `kubectl apply` during normal releases.

## URLs

| Surface | URL |
| --- | --- |
| Frontend | `https://calendar.212.2.249.45.nip.io` |
| Backend API | `https://calendar-api.212.2.249.45.nip.io` |

If the Civo ingress IP changes, update `k8s/base/configmap.yaml`, `k8s/base/ingress.yaml`, and the frontend repository build args.

## GitHub repository secrets

Configure these on `Evofront-Pvt-Ltd/Calendar-backend`:

| Secret | Required | Purpose |
| --- | --- | --- |
| `DOCKERHUB_USERNAME` | Yes | Docker Hub account used to publish images |
| `DOCKERHUB_PASSWORD` | Yes | Docker Hub password or access token |
| `KUBECONFIG` | Yes | Civo pods-cluster kubeconfig for rollout and pull secrets |

Application secrets (`JWT_SECRET`, SendGrid, Google) are created once in the cluster (step 3 below), not stored in GitHub Actions.

## One-time cluster bootstrap

Complete these steps on the shared Civo `pods-cluster` **before** creating Argo CD applications.

### 0. Create namespaces first

Namespaces must exist before secrets and before Argo CD syncs workloads.

```powershell
kubectl create namespace calendar-backend --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace calendar-frontend --dry-run=client -o yaml | kubectl apply -f -
kubectl get namespace calendar-backend calendar-frontend
```

Expected output: both namespaces show `Active`.

### 1. Confirm shared cluster prerequisites

```powershell
kubectl get ingressclass nginx
kubectl get clusterissuer letsencrypt-prod
kubectl get pods -A | Select-String "cert-manager|ingress-nginx"
```

If `letsencrypt-prod` is missing, apply `k8s/cluster/letsencrypt-prod-clusterissuer.yaml.template` once after replacing the admin email.

### 2. Create Docker Hub pull secret in the backend namespace

```powershell
kubectl create namespace calendar-backend --dry-run=client -o yaml | kubectl apply -f -
kubectl -n calendar-backend create secret docker-registry dockerhub-pull `
  --docker-server=https://index.docker.io/v1/ `
  --docker-username="<DOCKERHUB_USERNAME>" `
  --docker-password="<DOCKERHUB_PASSWORD>" `
  --dry-run=client -o yaml | kubectl apply -f -
```

### 3. Create application secrets

```powershell
kubectl -n calendar-backend create secret generic calendar-backend-secrets `
  --from-literal=JWT_SECRET="<strong-random-secret>" `
  --from-literal=SENDGRID_API_KEY="" `
  --from-literal=SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY="" `
  --from-literal=GOOGLE_CLIENT_SECRET="" `
  --from-literal=GOOGLE_TOKEN_ENCRYPTION_KEY="" `
  --dry-run=client -o yaml | kubectl apply -f -
```

### 4. Register the Argo CD application

```powershell
kubectl apply -f k8s/argocd/application.yaml
```

Or create the app in the Argo CD UI with:

- Repository: `https://github.com/Evofront-Pvt-Ltd/Calendar-backend.git`
- Branch: `develop`
- Path: `k8s/overlays/staging`
- Namespace: `calendar-backend`

### 5. Push to `develop`

The workflow will:

1. Run tests and container validation
2. Push `DOCKERHUB_USERNAME/calendar-backend:<full-git-sha>`
3. Roll out the SHA-tagged image via `kubectl` (no Git manifest commits)
4. Verify HTTPS health for API (`/health`) and frontend app URL

## Rollback

Preferred rollback is Git-based:

1. Revert the manifest commit in `k8s/overlays/staging/kustomization.yaml`, or
2. Set `newTag` to the previous known-good Git SHA and push

Argo CD will sync the previous image automatically.

Emergency cluster rollback:

```powershell
kubectl -n calendar-backend rollout undo deployment/calendar-backend
```

## Branch policy

Push CI/CD changes to `develop`.
