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
    # torch 先行安装（版本与本地开发一致 2.13.0，走清华源 CPU wheel）。
    # 理由：① requirements.txt 的 sentence-transformers 依赖 torch，不先装会隐式
    #          拉取巨型 wheel（境外源慢/易超时）；② GPU 稠密嵌入/重排由 Infinity
    #          (GPU 容器) 承担，镜像内 torch 仅用于稀疏向量等 CPU 侧推理；
    #        ③ 如需容器内 GPU torch，改用 aliyun pytorch-wheels 或 download.pytorch.org。
    pip install --no-cache-dir torch==2.13.0 && \
    pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY pipelines/ ./pipelines/

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
