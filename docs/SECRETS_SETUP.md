# Where to Put Calendar Backend Configuration

Never commit passwords, API keys, or JWT values in git. Use the locations below.

## Non-secret settings (safe in git)

These live in **`k8s/base/configmap.yaml`**. Argo CD deploys them automatically when you push to `develop`.

| Application variable | ConfigMap key | Example |
| --- | --- | --- |
| `EMAIL_ENABLED` | `EMAIL_ENABLED` | `true` |
| `EMAIL_PROVIDER` | `EMAIL_PROVIDER` | `sendgrid` |
| `SENDGRID_EMAIL_ENABLED` | `SENDGRID_EMAIL_ENABLED` | `true` |
| `SENDGRID_MAIL_SEND_URL` | `SENDGRID_MAIL_SEND_URL` | `https://api.sendgrid.com/v3/mail/send` |
| `SENDGRID_FROM_EMAIL` | `SENDGRID_FROM_EMAIL` | `mukesh.g@evofront.com` |
| `SENDGRID_FROM_NAME` | `SENDGRID_FROM_NAME` | `Calendar Booking` |
| `SENDGRID_REPLY_TO_EMAIL` | `SENDGRID_REPLY_TO_EMAIL` | `mukesh.g@evofront.com` |

Edit `k8s/base/configmap.yaml`, commit, push to `develop`, then sync in Argo CD.

## Secret settings (never in git)

Add these in **GitHub** only:

**Repository:** `Evofront-Pvt-Ltd/Calendar-backend`  
**Path:** Settings → Secrets and variables → Actions → New repository secret

| Dev team variable | GitHub secret name | Kubernetes secret key |
| --- | --- | --- |
| JWT / auth signing key | `CALENDAR_JWT_SECRET` | `JWT_SECRET` |
| `SENDGRID_API_KEY` | `CALENDAR_SENDGRID_API_KEY` | `SENDGRID_API_KEY` |
| SendGrid webhook public key | `CALENDAR_SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY` | `SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY` |
| Google OAuth client secret | `CALENDAR_GOOGLE_CLIENT_SECRET` | `GOOGLE_CLIENT_SECRET` |
| Google token encryption key | `CALENDAR_GOOGLE_TOKEN_ENCRYPTION_KEY` | `GOOGLE_TOKEN_ENCRYPTION_KEY` |

After adding or changing GitHub secrets, re-run **Actions → Calendar Backend CI/CD → deploy**.

The deploy job runs `ci-cd/scripts/ensure_backend_app_secrets.sh`, which creates/updates the Kubernetes secret **`calendar-backend-secrets`** in namespace **`calendar-backend`**.

If `CALENDAR_JWT_SECRET` is not set yet, the deploy script creates a **bootstrap** JWT secret in the cluster so the backend pod can start. Add the real JWT in GitHub and re-run deploy to replace it with a stable value.

## Manual cluster secret (alternative)

If you prefer not to use GitHub for app secrets, run once with pods-cluster kubeconfig:

```powershell
kubectl -n calendar-backend create secret generic calendar-backend-secrets `
  --from-literal=JWT_SECRET="<from-dev-team>" `
  --from-literal=SENDGRID_API_KEY="<from-dev-team>" `
  --from-literal=SENDGRID_EVENT_WEBHOOK_PUBLIC_KEY="" `
  --from-literal=GOOGLE_CLIENT_SECRET="" `
  --from-literal=GOOGLE_TOKEN_ENCRYPTION_KEY="" `
  --dry-run=client -o yaml | kubectl apply -f -
```

Then restart:

```powershell
kubectl -n calendar-backend rollout restart deployment/calendar-backend
```

## Checklist when dev team shares new values

1. **Public config** (emails, flags, URLs) → edit `k8s/base/configmap.yaml` → push `develop` → Argo CD sync
2. **API keys / JWT** → GitHub secret → re-run backend deploy workflow
3. **Never** paste secrets into `.env`, `configmap.yaml`, or workflow YAML files

## Security

If a secret was shared in chat, email, or a ticket, **rotate it** in SendGrid or your identity provider and update the GitHub secret with the new value.
