FROM python:3.10-slim

WORKDIR /app

# 使用国内 Debian 镜像源，加速系统依赖安装
RUN sed -i 's@deb.debian.org@mirrors.tuna.tsinghua.edu.cn@g' \
    /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    build-essential \
    git \
    xvfb \
    xauth \
    x11-utils \
    && rm -rf /var/lib/apt/lists/*

RUN python --version

COPY requirements.txt .

# 使用国内 PyPI 镜像源，加速 Python 依赖安装
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt

# 安装扫码登录所需的 Chromium 与系统依赖
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# 创建生产环境日志目录，运行时可挂载到宿主机持久化
RUN mkdir -p /data/logs/douyin-spider \
    && sed -i 's/\r$//' /app/docker-entrypoint.sh \
    && chmod +x /app/docker-entrypoint.sh

EXPOSE 5000

# 容器默认按生产环境启动，缺少安全配置时应直接失败。
ENV APP_ENV=prod \
    LOG_DIR=/data/logs/douyin-spider \
    QR_LOGIN_HEADLESS=false \
    PYTHONUNBUFFERED=1

# 显式启动虚拟显示，确保 Uvicorn 成为容器主进程。
CMD ["/app/docker-entrypoint.sh"]
