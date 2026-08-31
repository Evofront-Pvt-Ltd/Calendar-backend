#!/usr/bin/env bash
set -euo pipefail

IMAGE="${1:?image reference required}"

cleanup() {
  docker rm -f calendar-backend-validate calendar-mongo-validate >/dev/null 2>&1 || true
  docker network rm calendar-validate >/dev/null 2>&1 || true
}

trap cleanup EXIT

docker network create calendar-validate
docker run -d --name calendar-mongo-validate --network calendar-validate mongo:7

mongo_ready=0
for _ in $(seq 1 30); do
  if docker exec calendar-mongo-validate mongosh --quiet --eval 'db.runCommand({ ping: 1 }).ok'; then
    mongo_ready=1
    break
  fi
  sleep 2
done

if [ "${mongo_ready}" != "1" ]; then
  echo "::error::MongoDB sidecar did not become ready"
  docker logs calendar-mongo-validate
  exit 1
fi

# A throwaway strong secret, not a placeholder: the startup guard blocklists the
# known placeholders and only exempts development/test/local, so a literal here
# would fail startup and the container could never be validated.
VALIDATION_JWT_SECRET="$(openssl rand -hex 32)"

docker run -d --name calendar-backend-validate --network calendar-validate -p 127.0.0.1:8000:8000 \
  -e MONGODB_URL=mongodb://calendar-mongo-validate:27017 \
  -e MONGODB_DB=calendar_booking \
  -e JWT_SECRET="${VALIDATION_JWT_SECRET}" \
  -e ENVIRONMENT=ci \
  "${IMAGE}"

ready=0
for _ in $(seq 1 40); do
  if curl -fsS --max-time 5 http://127.0.0.1:8000/health | grep -q '"status":"ok"'; then
    ready=1
    break
  fi
  sleep 3
done

if [ "${ready}" != "1" ]; then
  echo "::error::Backend container did not serve GET /health"
  docker logs calendar-backend-validate
  exit 1
fi

if ! curl -fsS --max-time 5 http://127.0.0.1:8000/ | grep -q '"status":"ok"'; then
  echo "::error::Backend container did not serve GET /"
  docker logs calendar-backend-validate
  exit 1
fi
