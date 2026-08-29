#!/usr/bin/env bash
set -euo pipefail

: "${API_URL:?API_URL is required}"
: "${API_HEALTH_URL:?API_HEALTH_URL is required}"
: "${APP_URL:?APP_URL is required}"

{
  echo "## Deployed URLs"
  echo ""
  echo "| Surface | URL |"
  echo "| --- | --- |"
  echo "| Frontend | ${APP_URL} |"
  echo "| API | ${API_URL} |"
  echo "| Health | ${API_HEALTH_URL} |"
} >> "${GITHUB_STEP_SUMMARY}"
