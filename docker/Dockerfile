FROM node:24-bookworm-slim AS xhs-node
FROM python:3.10-slim

WORKDIR /app

# 使用国内 Debian 镜像源，加速系统依赖安装
# 系统依赖与浏览器虚拟显示环境
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

# 使用已验证的 Node 24 系列，避免发行版旧版本缺少 zlib.crc32。
COPY --from=xhs-node /usr/local/bin/node /usr/local/bin/node
COPY --from=xhs-node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY requirements.txt .

# 使用国内 PyPI 镜像源，加速 Python 依赖安装
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt

# 安装扫码登录所需的 Chromium 与系统依赖
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# 安装小红书指纹计算依赖，并检查 RAP 压缩所需运行时能力。
RUN npm ci --omit=dev --ignore-scripts \
    && node -e "require('crypto-js'); if(typeof require('zlib').crc32 !== 'function') process.exit(1)"

# 创建生产环境日志目录，运行时可挂载到宿主机持久化
RUN mkdir -p /data/logs/douyin-spider \
    && sed -i 's/\r$//' /app/docker/docker-entrypoint.sh \
    && chmod +x /app/docker/docker-entrypoint.sh

EXPOSE 5000

# 容器默认按生产环境启动，缺少安全配置时应直接失败。
ENV APP_ENV=prod \
    LOG_DIR=/data/logs/douyin-spider \
    QR_LOGIN_HEADLESS=false \
    PYTHONUNBUFFERED=1

# 显式启动虚拟显示，确保 Uvicorn 成为容器主进程。
CMD ["/app/docker/docker-entrypoint.sh"]
