#!/usr/bin/env bash
# Fast-path remote build —— FROM 上一镜像 + COPY 全部运行时 *.py + 注入 ENV。
# 适合改动只在 Python 源码(**不新增 pip 依赖**、不动 requirements)的迭代,
# build < 30s vs 完整构建 5-10min。
# 🔴 COPY 全部 *.py(不止 adapter.py/agentic_web.py)—— v0.6.0 B+ 教训:只 COPY 两文件
#    会在「加了新模块」时把它们漏在镜像外 → import 失败 → 功能静默退化(见 runbook
#    deploy-2026-06-21 「关键陷阱」)。根目录只有运行时模块,*.py 安全。
# ⚠️ 仍**不跑 pip install**:若本次新增了 pip 依赖,必须走 build_remote_image.sh 完整构建。
#
# 流程:
#   1. 本地 tar 源码 → OSS 上传(签名 URL)
#   2. ECS RunCommand 下载 + inline Dockerfile + build + push
#
# 用法:
#   ADAPTER_VERSION=v0.2.27 \
#   BASE_IMAGE=172.29.0.223:5000/lxj/adapter:v0.2.26-20260526 \
#   bash scripts/fast_path_build.sh
#
# 必须在 adapter repo 根目录运行(读 adapter.py / agentic_web.py / git rev-parse)。

set -euo pipefail

# ── 必填环境变量 ────────────────────────────────────────────────────
: "${ADAPTER_VERSION:?ADAPTER_VERSION 必填,如 v0.2.27}"
: "${BASE_IMAGE:?BASE_IMAGE 必填,如 172.29.0.223:5000/lxj/adapter:v0.2.26-20260526}"

# ── 自动推导 ────────────────────────────────────────────────────────
GIT_SHA=$(git rev-parse --short HEAD)
DATE=$(date +%Y%m%d)
NEW_TAG="172.29.0.223:5000/lxj/adapter:${ADAPTER_VERSION}-${DATE}"
SRC_TGZ="/tmp/adapter-${ADAPTER_VERSION}-src.tgz"
OSS_PATH="oss://lxj-ai-center/adapter/${ADAPTER_VERSION}/src.tgz"

echo "=== Fast-path build ==="
echo "  ADAPTER_VERSION  = $ADAPTER_VERSION"
echo "  ADAPTER_GIT_SHA  = $GIT_SHA"
echo "  BASE_IMAGE       = $BASE_IMAGE"
echo "  NEW_TAG          = $NEW_TAG"
echo

# ── 1. 打包源码上传 OSS ─────────────────────────────────────────────
tar -czf "$SRC_TGZ" *.py
echo "src tarball: $(du -h $SRC_TGZ | cut -f1) ($(ls -1 *.py | wc -l | tr -d ' ') modules)"
aliyun oss cp "$SRC_TGZ" "$OSS_PATH" --region cn-hangzhou --force >/dev/null
SIGNED_URL=$(aliyun oss sign "$OSS_PATH" --timeout 3600 --region cn-hangzhou 2>&1 | head -1)

# ── 2. ECS 上 build + push ──────────────────────────────────────────
ECS_INSTANCE="${ECS_INSTANCE:-i-bp1cobboogzx26knbucm}"

cat > /tmp/__fast_build.sh <<BUILD
set -e
cd /tmp
rm -rf __build_dir && mkdir __build_dir && cd __build_dir
curl -sS -o src.tgz '$SIGNED_URL'
tar -xzf src.tgz
cat > Dockerfile <<'DOCKERFILE'
FROM $BASE_IMAGE
COPY *.py /app/
ENV ADAPTER_VERSION=$ADAPTER_VERSION
ENV ADAPTER_GIT_SHA=$GIT_SHA
DOCKERFILE
docker build -t $NEW_TAG .
# 镜像内自检:确认全部运行时模块在镜像里且可导入 + 文件生成通路可用(命中"漏 COPY
# 模块"陷阱时 _FILE_GEN_AVAILABLE=False,在此 fail-fast,不推坏镜像)。
docker run --rm $NEW_TAG python -c "import adapter; assert adapter._FILE_GEN_AVAILABLE, 'FILE_GEN_UNAVAILABLE'; print('SELFCHECK_OK file_gen exts:', sorted(adapter._ARTIFACT_EXT_MIME))"
docker push $NEW_TAG
docker inspect $NEW_TAG --format '{{index .RepoDigests 0}}'
BUILD

B64=$(base64 -i /tmp/__fast_build.sh)
INVOKE_ID=$(aliyun ecs RunCommand --region cn-hangzhou \
  --InstanceId.1 "$ECS_INSTANCE" \
  --Type RunShellScript \
  --Name "adapter-${ADAPTER_VERSION}-fastbuild" \
  --CommandContent "$B64" \
  --ContentEncoding Base64 \
  --Timeout 600 2>&1 | python3 -c "import json,sys; print(json.load(sys.stdin).get('InvokeId'))")

echo "ECS InvokeId: $INVOKE_ID"
echo "Polling..."

until aliyun ecs DescribeInvocations --InvokeId "$INVOKE_ID" --region cn-hangzhou 2>&1 | \
  python3 -c "
import json,sys
s = (json.load(sys.stdin).get('Invocations',{}).get('Invocation') or [{}])[0].get('InvocationStatus')
print(s)
sys.exit(0 if s in ('Success','Failed','Stopped','Timeout','PartialFailed','Error') else 1)
"; do sleep 4; done

echo
echo "=== Build done. Output tail ==="
aliyun ecs DescribeInvocations --InvokeId "$INVOKE_ID" --IncludeOutput true --region cn-hangzhou 2>&1 | \
  python3 -c "
import json,sys,base64
d=json.load(sys.stdin)
i=(d.get('Invocations',{}).get('Invocation') or [{}])[0]
out=(i.get('InvokeInstances',{}).get('InvokeInstance',[{}])[0]).get('Output','')
print(base64.b64decode(out).decode('utf-8',errors='replace')[-800:])
"
echo
echo "=== Image pushed ==="
echo "  $NEW_TAG"
echo
echo "Next: deploy to SAE (注意!不带 --Envs,避免擦掉 secret env):"
echo "  aliyun sae DeployApplication \\"
echo "    --region cn-hangzhou \\"
echo "    --AppId bfe0ca22-a9a0-4738-a660-7b3e9193725d \\"
echo "    --ImageUrl '$NEW_TAG' \\"
echo "    --UpdateStrategy '{\"type\":\"BatchUpdate\",\"BatchUpdate\":{\"BatchWaitTime\":10,\"Batch\":1,\"ReleaseType\":\"auto\"}}'"
