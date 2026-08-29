# Calendar Backend CI/CD

Industrial GitOps deployment aligned with the Surveys pattern:

- **CI:** GitHub Actions validates, builds, and pushes Docker images to Docker Hub
- **CD:** Argo CD syncs `k8s/overlays/staging` from the `develop` branch
- **Traceability:** every release is pinned to a Git commit SHA in Kustomize

See [DEPLOY.md](../DEPLOY.md) for bootstrap, secrets, Argo CD registration, and rollback.

## Workflow

| Job | Trigger | Actions |
| --- | --- | --- |
| `ci` | PR and push to `develop` | unittest, docker build, container smoke test |
| `deploy` | push to `develop` only | push image to Docker Hub, commit manifest image tag |

## GitHub secrets

| Secret | Required |
| --- | --- |
| `DOCKERHUB_USERNAME` | Yes |
| `DOCKERHUB_PASSWORD` | Yes |

## Kubernetes layout

```text
k8s/
  base/                 # Namespace, MongoDB, backend deployment, service, ingress
  overlays/staging/     # Image tag updated by CI
  argocd/               # Argo CD Application manifest
  cluster/              # Optional ClusterIssuer template
  registry/             # Legacy in-cluster registry (not used by current pipeline)
```

## Hostnames

| Surface | URL |
| --- | --- |
| Frontend | `https://calendar.212.2.249.45.nip.io` |
| Backend API | `https://calendar-api.212.2.249.45.nip.io` |

## Branch

Push CI/CD changes to `develop`.
