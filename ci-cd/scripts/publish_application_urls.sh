#!/usr/bin/env bash
set -euo pipefail

: "${API_URL:?API_URL is required}"
: "${API_HEALTH_URL:?API_HEALTH_URL is required}"
: "${APP_URL:?APP_URL is required}"
: "${APP_HEALTH_URL:?APP_HEALTH_URL is required}"
: "${ARGOCD_URL:?ARGOCD_URL is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"

{
  echo "## Application URLs"
  echo ""
  echo "| Surface | URL |"
  echo "| --- | --- |"
  echo "| Frontend App | ${APP_URL} |"
  echo "| API | ${API_URL} |"
  echo "| API Health | ${API_HEALTH_URL} |"
  echo "| Argo CD | ${ARGOCD_URL} |"
  echo "| Image SHA | \`${GITHUB_SHA}\` |"
} >> "${GITHUB_STEP_SUMMARY}"
