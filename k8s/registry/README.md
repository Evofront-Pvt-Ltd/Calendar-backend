# Optional Docker registry ingress

Not one of the three Calendar platform services.

These manifests expose `calendar-registry.212.2.249.45.nip.io` for a private registry. They do not serve the Calendar API or frontend.

Application HTTPS:

- API: `k8s/services/calendar-backend/ingress.yaml`
- Frontend: Calendar-frontend repo ingress
