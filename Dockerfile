# ---------- 构建阶段：安装依赖 ----------
FROM python:3.12-slim AS builder

WORKDIR /build

# 先只拷贝依赖清单，充分利用 Docker 层缓存：
# 只要 requirements.txt 没变，改代码就不会重新装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ---------- 运行阶段：只带运行时需要的东西 ----------
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Doc Extractor" \
      org.opencontainers.image.description="文档 / 表格 / 图像结构化抽取：解析层 → 抽取层 → 校验层，同一份内容换格式存结果一致" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # 容器里用绝对路径，避免和镜像里的 WORKDIR 产生歧义
    DATA_DIR=/app/data

WORKDIR /app

COPY --from=builder /install /usr/local

# 只拷贝运行所需的东西。
# examples/ 是必须的 —— /api/examples 就是从这个目录读内置示例的。
COPY app/ ./app/
COPY web/ ./web/
COPY examples/ ./examples/
COPY .env.example README.md LICENSE ./

# 以非 root 用户运行；data/ 需要可写（上传的文件落盘在这里）
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 健康检查指向内置的 /api/health。
# 注意用 127.0.0.1 而不是 localhost：容器里 localhost 可能先解析到 ::1，
# 而 uvicorn 只监听了 IPv4 的 0.0.0.0，会导致健康检查假失败。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
