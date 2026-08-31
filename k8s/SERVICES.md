# Calendar platform services

## Count

| # | Service | Repo | Namespace | Pipeline | Argo CD app | Public URL |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `calendar-backend` | Calendar-backend | `calendar-backend` | `calendar-backend-ci-cd.yml` | `calendar-backend-staging` | `https://calendar-api.212.2.249.45.nip.io` |
| 2 | `calendar-mongodb` | Calendar-backend | `calendar-backend` (legacy PVC) or `calendar-mongodb` | `calendar-mongodb-ci-cd.yml` | `calendar-backend-staging` (umbrella app) | internal only |
| 3 | `calendar-frontend` | Calendar-frontend | `calendar-frontend` | `calendar-frontend-ci-cd.yml` | separate repo | `https://calendar.212.2.249.45.nip.io` |

**This repo deploys services 1 and 2 only.**

Service 3 is owned by the Calendar-frontend repository. Its namespace is bootstrapped here in `k8s/bootstrap/namespaces.yaml`.

## Layout in this repo

```text
k8s/services/calendar-backend/   service 1
k8s/services/calendar-mongodb/     service 2
k8s/overlays/staging/             umbrella overlay for Argo CD
k8s/argocd/application.yaml       single Argo app listing all services
```

## Legacy MongoDB note

The live cluster PVC currently lives in `calendar-backend`. Until migration, the MongoDB pipeline uses `calendar-mongodb-legacy` overlay. That is one service with two deployment paths, not a fourth service.

## Not a platform service

`k8s/registry/` is optional private Docker registry ingress only. It is not part of the three application services above.
