FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# 如无法访问清华镜像源，可改为默认 PyPI：
#   RUN pip install --no-cache-dir -r requirements.txt
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn && \
    # GPU torch 先行安装（与本地开发环境一致，CUDA 13.2）。
    # requirements.txt 的 sentence-transformers 依赖 torch，若不先装 GPU 版，
    # pip 会隐式拉取巨型 CPU wheel（下载慢/易超时）。
    # 无 GPU 环境可改为 CPU torch：pip install torch==2.13.0 --index-url https://download.pytorch.org/whl/cpu
    pip install --no-cache-dir torch==2.13.0 --index-url https://download.pytorch.org/whl/cu130 && \
    pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY pipelines/ ./pipelines/

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
