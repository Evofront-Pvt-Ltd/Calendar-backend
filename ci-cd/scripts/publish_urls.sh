#!/usr/bin/env bash
set -euo pipefail

: "${API_HEALTH_URL:?API_HEALTH_URL is required}"
: "${APP_URL:?APP_URL is required}"

echo "Frontend: ${APP_URL}"
echo "Health: ${API_HEALTH_URL}"
