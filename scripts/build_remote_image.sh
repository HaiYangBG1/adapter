#!/usr/bin/env bash
# Full-source build(从 Dockerfile 完整构建)—— pip install + Playwright install
# 都跑一遍,产物是 4-5GB 大镜像。后续小改动用 fast-path build(inline Dockerfile
# FROM 上一镜像 + COPY *.py + 注入 args)更快,见 README / runbooks。
#
# v0.2.27:build 时显式注入 ADAPTER_GIT_SHA + ADAPTER_VERSION,让 /health
# 报告真实版本(之前 fast-path 继承 base image env,sha 一直停在 ba03af0)。
set -euo pipefail

IMAGE_TAG="${1:-localhost:5000/adapter:latest}"
REPO_PATH="${IMAGE_TAG#*/}"
REPO_PATH="${REPO_PATH%:*}"

# 自动从 git 拿当前 short sha;ADAPTER_VERSION 仍是 env(由 caller 指定如
# v0.2.27),caller 不传时 build 报 unknown,避免静默错误。
GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
VERSION="${ADAPTER_VERSION:-unknown}"

echo "Building $IMAGE_TAG"
echo "  ADAPTER_GIT_SHA  = $GIT_SHA"
echo "  ADAPTER_VERSION  = $VERSION"

docker build --pull \
  --build-arg "ADAPTER_GIT_SHA=$GIT_SHA" \
  --build-arg "ADAPTER_VERSION=$VERSION" \
  -t "$IMAGE_TAG" .
docker push "$IMAGE_TAG"
curl -k -s "https://localhost:5000/v2/${REPO_PATH}/tags/list" || true
