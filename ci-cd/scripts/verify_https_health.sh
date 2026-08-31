#!/usr/bin/env bash
set -euo pipefail

: "${API_HEALTH_URL:?API_HEALTH_URL is required}"
: "${APP_HEALTH_URL:?APP_HEALTH_URL is required}"
api_ok=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 15 "${API_HEALTH_URL}" | grep -q '"status":"ok"'; then
    api_ok=1
    break
  fi
  sleep 10
done
if [ "${api_ok}" != "1" ]; then
  echo "::error::API HTTPS health check failed: ${API_HEALTH_URL}"
  exit 1
fi
echo "API HTTPS health OK: ${API_HEALTH_URL}"

app_ok=0
for _ in $(seq 1 30); do
  code=$(curl -fsS --max-time 15 -o /dev/null -w "%{http_code}" "${APP_HEALTH_URL}")
  if [ "${code}" -ge 200 ] && [ "${code}" -lt 400 ]; then
    app_ok=1
    break
  fi
  sleep 10
done
if [ "${app_ok}" != "1" ]; then
  echo "::error::App HTTPS health check failed: ${APP_HEALTH_URL} (HTTP ${code:-000})"
  exit 1
fi
echo "App HTTPS health OK: ${APP_HEALTH_URL}"
