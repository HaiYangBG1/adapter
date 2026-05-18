#!/usr/bin/env bash
set -euo pipefail

IMAGE_TAG="${1:-localhost:5000/adapter:latest}"
REPO_PATH="${IMAGE_TAG#*/}"
REPO_PATH="${REPO_PATH%:*}"

docker build --pull -t "$IMAGE_TAG" .
docker push "$IMAGE_TAG"
curl -k -s "https://localhost:5000/v2/${REPO_PATH}/tags/list" || true
