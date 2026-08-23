#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# 冻结/补齐 embedding-service（Dockerfile gpu 阶段）离线 wheelhouse
#
# 用途：把 requirements-gpu.txt + torch(GPU/CPU) 及其全部传递依赖的
#       预编译 wheel 一次性下载到 build_cache/wheels/。此后构建
#       embedding-service 时 gpu 阶段全离线安装（--no-index
#       --find-links /wheels），不再联网拉包、不再卡在清华源。
#
# 用法（仓库根目录执行，或任意目录执行本脚本的绝对路径）：
#   bash scripts/freeze_wheels.sh          # 冻结/补齐（幂等）
#   scripts/start.sh wheels                # 同上（start.sh 子命令）
#
# 幂等性：完整性预检（容器内离线 dry-run 解析）通过即跳过下载；
#         requirements-gpu.txt 或 torch 版本变更后重跑即可补齐。
#
# 说明：
#   - 必须在 python:3.11-slim 容器内执行 pip download——宿主是 py3.10，
#     只有容器才能保证 wheel 平台与目标镜像（cp311 / manylinux）一致。
#   - --only-binary=:all: 保证只下载预编译 wheel；缺编译链路的包直接报错点名。
#   - wheelhouse 已被 .gitignore 排除，不进 git；Dockerfile 用 bind mount
#     只读注入，也不进任何镜像层。
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

WHEEL_DIR="$REPO_ROOT/build_cache/wheels"
REQ_GPU="$REPO_ROOT/requirements-gpu.txt"
PY_IMAGE="python:3.11-slim"
INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"

# 与 Dockerfile gpu 阶段 TORCH_INSTALL 保持一致；GPU 变体 wheel 走 aliyun 单文件源
TORCH_GPU_VER="torch==2.13.0+cu126"
TORCH_GPU_WHEEL="torch-2.13.0+cu126-cp311-cp311-manylinux_2_28_x86_64.whl"
TORCH_GPU_URL="https://mirrors.aliyun.com/pytorch-wheels/cu126/torch-2.13.0%2Bcu126-cp311-cp311-manylinux_2_28_x86_64.whl"

info() { echo -e "\033[36m[i]\033[0m $*"; }
ok()   { echo -e "\033[32m[✓]\033[0m $*"; }
fail() { echo -e "\033[31m[x]\033[0m $*" >&2; exit 1; }

command -v docker >/dev/null || fail "缺少 docker 命令"
[[ -f "$REQ_GPU" ]] || fail "缺少 $REQ_GPU"
mkdir -p "$WHEEL_DIR"

# ── 完整性预检：离线解析是否已满足 gpu 构建需求 ───────────────────
# 容器内 dry-run；若全部 wheel 已在 /wheels → 退出码 0 → 跳过下载
info "预检 wheelhouse 是否已满足 $REQ_GPU + $TORCH_GPU_VER ..."
if docker run --rm \
    -v "$WHEEL_DIR:/wheels" \
    -v "$REQ_GPU:/req.txt:ro" \
    "$PY_IMAGE" \
    sh -c "pip install --dry-run --ignore-installed --no-index --find-links /wheels -r /req.txt '$TORCH_GPU_VER' >/dev/null 2>&1"; then
    ok "wheelhouse 已完整（$WHEEL_DIR）"
else
    info "wheelhouse 不完整，开始离线冻结下载（首次约需数分钟）..."

    docker run --rm \
        -v "$WHEEL_DIR:/wheels" \
        -v "$REQ_GPU:/req.txt:ro" \
        "$PY_IMAGE" \
        sh -c "
            set -euo pipefail
            pip config set global.index-url '$INDEX_URL' >/dev/null
            pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn >/dev/null
            pip install -q -U pip
            cd /wheels
            # 1) GPU torch wheel（aliyun 单文件；缺失才下载，843MB）
            if [ ! -f '$TORCH_GPU_WHEEL' ]; then
                echo '[i] 下载 $TORCH_GPU_WHEEL ...'
                python3 -c \"import urllib.request; urllib.request.urlretrieve('$TORCH_GPU_URL', '$TORCH_GPU_WHEEL')\"
            fi
            # 2) requirements-gpu.txt 全量闭包（仅预编译 wheel）
            #    sentence-transformers 的 torch 依赖会解析为 CPU 变体，正好补齐降级路径
            echo '[i] 下载 requirements-gpu.txt 闭包 ...'
            pip download --no-cache-dir --only-binary=:all: -d /wheels -r /req.txt
            # 3) GPU torch 的 nvidia-* 依赖闭包（从本地 wheel 元数据解析）
            echo '[i] 下载 GPU torch 的 nvidia-* 依赖闭包 ...'
            pip download --no-cache-dir --only-binary=:all: -d /wheels './$TORCH_GPU_WHEEL'
            # 4) CPU torch 变体闭包（Dockerfile TORCH_INSTALL 降级路径用）
            echo '[i] 下载 CPU torch 变体闭包 ...'
            pip download --no-cache-dir --only-binary=:all: -d /wheels 'torch==2.13.0'
        "
    ok "wheelhouse 冻结完成"
fi

echo ""
info "wheelhouse 现状：$(ls "$WHEEL_DIR" | wc -l) 个 wheel，$(du -sh "$WHEEL_DIR" | cut -f1)"
info "提示：build_cache/ 根目录的 torch-2.13.0+cu126-....whl（843MB）与 wheels/ 内同名 wheel 重复，可手动删除。"
