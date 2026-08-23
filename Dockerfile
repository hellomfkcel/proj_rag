# syntax=docker/dockerfile:1
# ════════════════════════════════════════════════════════════════
# RAG v14 — 多阶段 Dockerfile（builder / base / milvus / web / haystack / gpu）
#
# 按服务依赖分层，让每个服务只携带自己需要的依赖：
#   builder   编译阶段（含 build-essential，把 base 依赖装进 venv）
#   base      真共享运行镜像（无 web/无 pymilvus）→ visibility-events
#   milvus    base + pymilvus + JWT → stamping-worker / outbox-relay
#   web       milvus + api 特有 → api
#   haystack  milvus + Haystack 检索/摄入栈（无 torch）→ ingestion / retrieval
#   gpu       base + FastAPI + GPU torch + 模型 → embedding-service（唯一持有模型）
#
# compose 中用 build.target 指定各服务使用哪个阶段。
# 需要 BuildKit 的 RUN --mount（bind mount 加载离线 wheelhouse）。
# ════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════
# Stage 0: builder — 编译 base 依赖进 venv（build-essential 只在此阶段）
# ════════════════════════════════════════════════════════════════
FROM python:3.11-slim AS builder

WORKDIR /app

ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

# apt 换国内源（默认 deb.debian.org 在国内极慢）；sed 兼容 deb822 / legacy 两种格式
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 全部 base 依赖装进独立 venv，供干净运行镜像整体拷贝
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements-base.txt ./
RUN pip install --no-cache-dir -r requirements-base.txt && \
    # 文件编码检测（src/platform/store/encoding.py 运行时 import）
    pip install --no-cache-dir chardet==7.5.1

# ════════════════════════════════════════════════════════════════
# Stage 1: base — 真共享运行镜像（无编译工具，无 web/milvus 依赖）
# ════════════════════════════════════════════════════════════════
FROM python:3.11-slim AS base

WORKDIR /app

# 统一清华镜像源（供下游 milvus/web/haystack/gpu 阶段 pip install 继承）
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

# 仅装运行时系统库；不需要 git/build-essential（pip 依赖已编译好）
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 拷贝已编译好的 venv（含全部 base 依赖 + chardet）
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY src/ ./src/
COPY pipelines/ ./pipelines/
# 建表 SQL（init_db 依赖 scripts/init.sql）
COPY scripts/init.sql ./scripts/init.sql

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ════════════════════════════════════════════════════════════════
# Stage 2: milvus — base + 直连 Milvus + JWT
# ════════════════════════════════════════════════════════════════
FROM base AS milvus

USER root
COPY requirements-milvus.txt ./
RUN pip install --no-cache-dir -r requirements-milvus.txt
USER appuser

# ════════════════════════════════════════════════════════════════
# Stage 3: web — milvus + api 特有依赖
# ════════════════════════════════════════════════════════════════
FROM milvus AS web

USER root
COPY requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt
USER appuser

# ════════════════════════════════════════════════════════════════
# Stage 4: haystack — milvus + Haystack 检索/摄入栈（无 torch）
# ════════════════════════════════════════════════════════════════
FROM milvus AS haystack

USER root
COPY requirements-haystack.txt ./
# 说明：worker 不安装 torch——嵌入/重排全部经 embedding-service HTTP。
#       haystack-ai 的 torch 导入均由 LazyImport 包裹，缺失时优雅延迟。
RUN pip install --no-cache-dir -r requirements-haystack.txt
USER appuser

# ════════════════════════════════════════════════════════════════
# Stage 5: gpu — base + FastAPI + GPU torch + 模型（仅 embedding-service）
# ════════════════════════════════════════════════════════════════
FROM base AS gpu

# 构建参数：torch 变体。
#   torch==2.13.0+cu126 → GPU torch + nvidia CUDA 栈（BGE-M3 一次前向 dense+sparse）
#   torch==2.13.0        → CPU torch（GPU 不可用时的降级）
# 两种 wheel 均在 build_cache/wheels/（宿主机预下载，离线安装）。
ARG TORCH_INSTALL="torch==2.13.0+cu126"

USER root
# 在线安装大量原生包（transformers/tokenizers/scipy/scikit-learn…），
# 多数有 manylinux wheel，但缺轮时需编译——此处保留编译工具兜底
# （gpu 镜像本就 ~11GB，+350MB 编译工具可接受；仅此阶段需要）。
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-gpu.txt ./
# torch 依赖 wheelhouse 预下载到宿主机 build_cache/wheels/（国内源，避免构建时
# buildkit 对超大文件下载停滞）。生成方式（宿主机执行，网络快）：
#   mkdir -p build_cache && curl -o "build_cache/torch-2.13.0+cu126-cp311-cp311-manylinux_2_28_x86_64.whl" \
#     "https://mirrors.aliyun.com/pytorch-wheels/cu126/torch-2.13.0%2Bcu126-cp311-cp311-manylinux_2_28_x86_64.whl"
#   pip download "build_cache/torch-2.13.0+cu126-cp311-cp311-manylinux_2_28_x86_64.whl" \
#     -d build_cache/wheels/ --index-url https://pypi.tuna.tsinghua.edu.cn/simple
#   pip download torch==2.13.0 --no-deps -d build_cache/wheels/ \
#     --index-url https://pypi.tuna.tsinghua.edu.cn/simple
# 说明：cu130 国内镜像未同步 2.13.0，用 cu126（驱动向下兼容）。
# torch 离线安装（先于 sentence-transformers/FlagEmbedding，满足其 torch 依赖，
# 避免 pip 在线回溯超大 torch wheel）。
# 用 BuildKit bind mount 加载 wheelhouse：wheels 不进任何镜像层
# （此前 COPY + rm 会把 3.9GB 的 wheel 层永久留在镜像里）。
RUN --mount=type=bind,source=build_cache/wheels,target=/wheels \
    pip install --no-index --find-links /wheels ${TORCH_INSTALL} && \
    pip install --no-index --find-links /wheels -r requirements-gpu.txt
USER appuser
