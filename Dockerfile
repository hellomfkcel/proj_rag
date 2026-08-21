FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl git \
    && rm -rf /var/lib/apt/lists/*

# GPU torch 依赖 wheelhouse 预下载到宿主机 build_cache/wheels/（国内源，避免构建时
# buildkit 对超大文件下载停滞）。生成方式（宿主机执行，网络快）：
#   mkdir -p build_cache && curl -o "build_cache/torch-2.13.0+cu126-cp311-cp311-manylinux_2_28_x86_64.whl" \
#     "https://mirrors.aliyun.com/pytorch-wheels/cu126/torch-2.13.0%2Bcu126-cp311-cp311-manylinux_2_28_x86_64.whl"
#   pip download "build_cache/torch-2.13.0+cu126-cp311-cp311-manylinux_2_28_x86_64.whl" \
#     -d build_cache/wheels/ --index-url https://pypi.tuna.tsinghua.edu.cn/simple
# 说明：摄入 embedding 默认走本地 BGE-M3 一次前向(dense+sparse)，必须 GPU torch；
#       cu130 国内镜像未同步 2.13.0，用 cu126（驱动向下兼容）。
COPY requirements.txt ./
COPY build_cache/wheels/ /wheels/
# 如无法访问清华镜像源，可改为默认 PyPI：
#   RUN pip install --no-cache-dir -r requirements.txt
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn && \
    # torch + nvidia 依赖离线安装（wheelhouse 已含完整闭包），避免构建时大文件下载停滞
    pip install --no-index --find-links /wheels torch==2.13.0+cu126 && \
    rm -rf /wheels && \
    pip install --no-cache-dir -r requirements.txt

# 文件编码检测（src/platform/store/encoding.py 运行时 import；独立层避免重跑大依赖层）
RUN pip install --no-cache-dir chardet==7.5.1

COPY src/ ./src/
COPY pipelines/ ./pipelines/
# 建表 SQL（init_db 依赖 scripts/init.sql）
COPY scripts/init.sql ./scripts/init.sql

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
