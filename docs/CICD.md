# Calendar Backend CI/CD

This repository deploys the Calendar backend and its MongoDB dependency. It also contains the one-time private registry manifests for the existing Pods Civo cluster.

## Hostnames

These hostnames follow the existing Pods Civo ingress IP `212.2.249.45` and [nip.io](https://nip.io/) resolution used by `Evofront-Pvt-Ltd/Pods-Frontend`.

| Surface | URL |
| --- | --- |
| Frontend | `https://calendar.212.2.249.45.nip.io` |
| Backend API | `https://calendar-api.212.2.249.45.nip.io` |
| Private registry | `https://calendar-registry.212.2.249.45.nip.io` |

If the Civo load-balancer IP changes, update ingress manifests, the backend ConfigMap CORS/frontend URLs, and `.github/workflows/calendar-backend-ci-cd.yml`.

## GitHub secrets

Create these repository secrets on `Evofront-Pvt-Ltd/Calendar-backend`:

| Secret | Required | Purpose |
| --- | --- | --- |
| `KUBECONFIG` | Yes | Existing Pods Civo kubeconfig, raw YAML or base64 YAML |
| `REGISTRY_USERNAME` | Yes | htpasswd username for the in-cluster registry |
| `REGISTRY_PASSWORD` | Yes | htpasswd password for the in-cluster registry |
| `CALENDAR_JWT_SECRET` | Yes | FastAPI `JWT_SECRET` |
| `CALENDAR_SENDGRID_API_KEY` | No | SendGrid API key; empty keeps email disabled |
| `CALENDAR_SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY` | No | SendGrid event webhook public key |
| `CALENDAR_GOOGLE_CLIENT_SECRET` | No | Google OAuth client secret; Google Calendar remains disabled unless enabled later |
| `CALENDAR_GOOGLE_TOKEN_ENCRYPTION_KEY` | No | Fernet key for stored Google tokens |

`GITHUB_TOKEN` is sufficient for checkout. No extra GitHub PAT is required.

## Existing cluster prerequisites

Confirm these already exist on the Pods Civo cluster before the first Calendar deploy:

```powershell
kubectl get ns
kubectl get ingressclass nginx
kubectl get clusterissuer letsencrypt-prod
kubectl get pods -A | Select-String "cert-manager|ingress-nginx"
```

Do not install a second cert-manager. Do not create another Kubernetes cluster.

If `letsencrypt-prod` is missing, copy `k8s/cluster/letsencrypt-prod-clusterissuer.yaml.template`, replace `REPLACE_WITH_ADMIN_EMAIL`, and apply that file once.

If cert-manager itself is missing:

```powershell
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.2/cert-manager.yaml
```

## Private registry bootstrap

Generate credentials locally. Do not put the password in Git.

```powershell
$registryUser = "calendar-ci"
$registryPassword = -join ((48..57 + 65..90 + 97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ })
$httpSecret = -join ((48..57 + 97..102) | Get-Random -Count 64 | ForEach-Object { [char]$_ })
docker run --rm httpd:2.4-alpine htpasswd -Bbn $registryUser $registryPassword | Out-File -Encoding ascii htpasswd.tmp
kubectl apply -f k8s/registry/namespace.yaml
kubectl -n calendar create secret generic calendar-registry-auth --from-file=htpasswd=htpasswd.tmp
kubectl -n calendar create secret generic calendar-registry-http --from-literal=secret=$httpSecret
Remove-Item htpasswd.tmp
kubectl apply -f k8s/registry/pvc.yaml
kubectl apply -f k8s/registry/service.yaml
kubectl apply -f k8s/registry/ingress.yaml
kubectl apply -f k8s/registry/deployment.yaml
kubectl -n calendar rollout status deployment/calendar-registry --timeout=180s
```

Store `$registryUser` / `$registryPassword` as `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` in both Calendar GitHub repositories. Never echo those values into logs.

Validate TLS and authentication:

```powershell
curl.exe -I https://calendar-registry.212.2.249.45.nip.io/v2/
```

Expect HTTP 401 and a Let's Encrypt certificate. Do not use `--insecure`.

## Obtaining kubeconfig

In the Civo dashboard, open the existing Pods Kubernetes cluster and download kubeconfig. Store the file contents as the `KUBECONFIG` GitHub Actions secret. Do not commit the file. Do not print it.

Recommended later hardening: issue a kubeconfig for the `calendar-cicd` ServiceAccounts in `k8s/rbac.yaml` and `k8s/registry/rbac.yaml` instead of using a cluster-admin kubeconfig.

## Branch

Push CI/CD changes to `develop`. Do not change the default branch.
