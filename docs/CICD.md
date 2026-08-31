# Calendar Backend CI/CD

See [k8s/SERVICES.md](../k8s/SERVICES.md) for the exact service count and mapping.

## This repo

| Service | Namespace | Workflow |
| --- | --- | --- |
| `calendar-backend` | `calendar-backend` | `calendar-backend-ci-cd.yml` |
| `calendar-mongodb` | `calendar-mongodb` | `calendar-mongodb-ci-cd.yml` |

Service `calendar-frontend` lives in the Calendar-frontend repository.

## API HTTPS

`k8s/services/calendar-backend/ingress.yaml` opens `https://calendar-api.212.2.249.45.nip.io`.

Push CI/CD changes to `develop`.
