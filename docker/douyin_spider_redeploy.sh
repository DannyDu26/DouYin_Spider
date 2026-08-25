#!/bin/sh
set -eu

# 可通过环境变量覆盖以下部署参数。
APP_ROOT=${APP_ROOT:-/data/app}
DEPLOY_DIR=${DEPLOY_DIR:-"${APP_ROOT}/DouYin_Spider"}
BACKUP_ROOT=${BACKUP_ROOT:-"${APP_ROOT}/backup"}
ARCHIVE_PATH=${1:-"${APP_ROOT}/DouYin_Spider.zip"}
ENV_FILE=${ENV_FILE:-"${DEPLOY_DIR}/.env.prod"}
IMAGE_NAME=${IMAGE_NAME:-douyin-spider}
CONTAINER_NAME=${CONTAINER_NAME:-douyin-spider}
HOST_PORT=${HOST_PORT:-5000}
CONTAINER_PORT=${CONTAINER_PORT:-5000}
RESTART_POLICY=${RESTART_POLICY:-unless-stopped}
LOG_DRIVER=${LOG_DRIVER:-local}
HOST_LOG_DIR=${HOST_LOG_DIR:-/data/logs/douyin-spider}
CONTAINER_LOG_DIR=${CONTAINER_LOG_DIR:-/data/logs/douyin-spider}
STARTUP_TIMEOUT=${STARTUP_TIMEOUT:-60}

TEMP_DIR=''
CANDIDATE_IMAGE=''
BACKUP_CONTAINER=''
BACKUP_SOURCE_DIR=''
OLD_CONTAINER_MOVED=false
SOURCE_BACKED_UP=false
SOURCE_INSTALLED=false
DEPLOYMENT_COMPLETE=false

log() {
    printf '[redeploy] %s\n' "$*"
}

fail() {
    printf '[redeploy] 错误：%s\n' "$*" >&2
    exit 1
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

container_exists() {
    docker container inspect "$1" >/dev/null 2>&1
}

cleanup() {
    exit_code=$?
    trap - EXIT HUP INT TERM

    # 部署中断时恢复原容器。
    if [ "${DEPLOYMENT_COMPLETE}" != 'true' ] && [ "${OLD_CONTAINER_MOVED}" = 'true' ]; then
        log '新版本部署失败，正在恢复旧容器'
        if container_exists "${CONTAINER_NAME}"; then
            docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
        fi
        docker rename "${BACKUP_CONTAINER}" "${CONTAINER_NAME}" >/dev/null 2>&1 || true
        docker start "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    fi

    # 部署失败时移除新目录，并按需恢复原项目目录。
    if [ "${DEPLOYMENT_COMPLETE}" != 'true' ] && [ "${SOURCE_INSTALLED}" = 'true' ]; then
        if [ -e "${DEPLOY_DIR}" ]; then
            if [ -n "${TEMP_DIR}" ] && [ -d "${TEMP_DIR}" ]; then
                mv "${DEPLOY_DIR}" "${TEMP_DIR}/failed-deployment" >/dev/null 2>&1 || true
            fi
        fi
    fi
    if [ "${DEPLOYMENT_COMPLETE}" != 'true' ] && [ "${SOURCE_BACKED_UP}" = 'true' ]; then
        log '正在恢复原项目目录'
        if [ ! -e "${DEPLOY_DIR}" ] && [ -d "${BACKUP_SOURCE_DIR}" ]; then
            mv "${BACKUP_SOURCE_DIR}" "${DEPLOY_DIR}" >/dev/null 2>&1 || true
        fi
    fi

    if [ -n "${TEMP_DIR}" ] && [ -d "${TEMP_DIR}" ]; then
        rm -rf -- "${TEMP_DIR}"
    fi
    if [ "${DEPLOYMENT_COMPLETE}" != 'true' ] && [ -n "${CANDIDATE_IMAGE}" ]; then
        docker image rm "${CANDIDATE_IMAGE}" >/dev/null 2>&1 || true
    fi

    exit "${exit_code}"
}

trap cleanup EXIT
trap 'exit 1' HUP INT TERM

command_exists docker || fail '未找到 docker 命令'
command_exists unzip || fail '未找到 unzip 命令'
command_exists mktemp || fail '未找到 mktemp 命令'
[ -d "${APP_ROOT}" ] || fail "应用根目录不存在：${APP_ROOT}"
[ ! -e "${DEPLOY_DIR}" ] || [ -d "${DEPLOY_DIR}" ] || fail "项目路径不是目录：${DEPLOY_DIR}"
[ -f "${ARCHIVE_PATH}" ] || fail "压缩包不存在：${ARCHIVE_PATH}"

EXISTING_SOURCE=false
if [ -d "${DEPLOY_DIR}" ]; then
    EXISTING_SOURCE=true
fi

# 项目外配置始终预先校验；项目内配置在解压后再校验。
case "${ENV_FILE}" in
    "${DEPLOY_DIR}"/*)
        if [ "${EXISTING_SOURCE}" = 'true' ]; then
            [ -f "${ENV_FILE}" ] || fail "生产配置不存在：${ENV_FILE}"
        fi
        ;;
    *) [ -f "${ENV_FILE}" ] || fail "生产配置不存在：${ENV_FILE}" ;;
esac

case "${ARCHIVE_PATH}" in
    "${DEPLOY_DIR}"/*) fail '压缩包不能放在待备份的项目目录内，请放到 /data/app 下' ;;
esac
case "${STARTUP_TIMEOUT}" in
    ''|*[!0-9]*) fail 'STARTUP_TIMEOUT 必须是正整数' ;;
    0) fail 'STARTUP_TIMEOUT 必须大于 0' ;;
esac

docker info >/dev/null 2>&1 || fail 'Docker 服务不可用'
unzip -tq "${ARCHIVE_PATH}" >/dev/null || fail '压缩包校验失败'

DEPLOY_ID=$(date '+%Y%m%d%H%M%S')-$$
BACKUP_SOURCE_DIR="${BACKUP_ROOT}/DouYin_Spider-${DEPLOY_ID}"
BACKUP_CONTAINER="${CONTAINER_NAME}-rollback-${DEPLOY_ID}"
CANDIDATE_IMAGE="${IMAGE_NAME}:deploy-${DEPLOY_ID}"

# 非首次部署时先备份原项目，时间戳用于保留历史版本。
if [ "${EXISTING_SOURCE}" = 'true' ]; then
    mkdir -p "${BACKUP_ROOT}"
    [ ! -e "${BACKUP_SOURCE_DIR}" ] || fail "备份目录已存在：${BACKUP_SOURCE_DIR}"
    log "正在备份原项目到 ${BACKUP_SOURCE_DIR}"
    mv "${DEPLOY_DIR}" "${BACKUP_SOURCE_DIR}"
    SOURCE_BACKED_UP=true
else
    log "未发现原项目目录，将执行首次部署：${DEPLOY_DIR}"
fi

# 在同一磁盘解压并识别项目根目录。
TEMP_DIR=$(mktemp -d "${APP_ROOT}/.douyin-spider-redeploy.XXXXXX")
EXTRACT_DIR="${TEMP_DIR}/source"
mkdir -p "${EXTRACT_DIR}"
log "正在解压 ${ARCHIVE_PATH}"
unzip -q "${ARCHIVE_PATH}" -d "${EXTRACT_DIR}"

if [ -f "${EXTRACT_DIR}/docker/Dockerfile" ]; then
    NEW_SOURCE_DIR=${EXTRACT_DIR}
else
    DOCKERFILE_COUNT=$(find "${EXTRACT_DIR}" -mindepth 3 -maxdepth 4 -type f -path '*/docker/Dockerfile' | wc -l | tr -d ' ')
    [ "${DOCKERFILE_COUNT}" = '1' ] || fail "压缩包中应包含且仅包含一个项目 docker/Dockerfile，当前找到 ${DOCKERFILE_COUNT} 个"
    DOCKERFILE_PATH=$(find "${EXTRACT_DIR}" -mindepth 3 -maxdepth 4 -type f -path '*/docker/Dockerfile' | head -n 1)
    NEW_SOURCE_DIR=$(dirname -- "$(dirname -- "${DOCKERFILE_PATH}")")
fi

mv "${NEW_SOURCE_DIR}" "${DEPLOY_DIR}"
SOURCE_INSTALLED=true

# 项目内配置：再次部署从备份复制，首次部署使用压缩包内配置。
case "${ENV_FILE}" in
    "${DEPLOY_DIR}"/*)
        if [ "${SOURCE_BACKED_UP}" = 'true' ]; then
            ENV_RELATIVE_PATH=${ENV_FILE#"${DEPLOY_DIR}/"}
            BACKUP_ENV_FILE="${BACKUP_SOURCE_DIR}/${ENV_RELATIVE_PATH}"
            [ -f "${BACKUP_ENV_FILE}" ] || fail "备份中的生产配置不存在：${BACKUP_ENV_FILE}"
            mkdir -p "$(dirname -- "${ENV_FILE}")"
            cp -p "${BACKUP_ENV_FILE}" "${ENV_FILE}"
        fi
        [ -f "${ENV_FILE}" ] || fail "生产配置不存在：${ENV_FILE}；首次部署请将配置放入压缩包，或通过 ENV_FILE 指定外部配置"
        ;;
esac

log "正在构建镜像 ${CANDIDATE_IMAGE}"
docker build -f "${DEPLOY_DIR}/docker/Dockerfile" -t "${CANDIDATE_IMAGE}" "${DEPLOY_DIR}"

# 先保留旧容器，便于启动失败时快速回滚。
if container_exists "${CONTAINER_NAME}"; then
    log "正在停止旧容器 ${CONTAINER_NAME}"
    docker stop "${CONTAINER_NAME}" >/dev/null
    docker rename "${CONTAINER_NAME}" "${BACKUP_CONTAINER}"
    OLD_CONTAINER_MOVED=true
fi

log "正在启动新容器 ${CONTAINER_NAME}"
docker run -d \
    --name "${CONTAINER_NAME}" \
    --restart "${RESTART_POLICY}" \
    --log-driver "${LOG_DRIVER}" \
    --publish "${HOST_PORT}:${CONTAINER_PORT}" \
    --env-file "${ENV_FILE}" \
    --volume "${HOST_LOG_DIR}:${CONTAINER_LOG_DIR}" \
    "${CANDIDATE_IMAGE}" >/dev/null

# 等待 API 健康检查通过。
attempt=0
while [ "${attempt}" -lt "${STARTUP_TIMEOUT}" ]; do
    if docker exec "${CONTAINER_NAME}" \
        curl -fsS --max-time 3 "http://127.0.0.1:${CONTAINER_PORT}/api/health" >/dev/null 2>&1; then
        break
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null || true)" != 'true' ]; then
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done

if [ "${attempt}" -ge "${STARTUP_TIMEOUT}" ] || ! docker exec "${CONTAINER_NAME}" \
    curl -fsS --max-time 3 "http://127.0.0.1:${CONTAINER_PORT}/api/health" >/dev/null 2>&1; then
    log '新容器健康检查失败，最近日志如下：'
    docker logs --tail 100 "${CONTAINER_NAME}" >&2 || true
    fail '新版本未能正常启动'
fi

docker tag "${CANDIDATE_IMAGE}" "${IMAGE_NAME}:latest"
if [ "${OLD_CONTAINER_MOVED}" = 'true' ]; then
    docker rm "${BACKUP_CONTAINER}" >/dev/null || log "警告：旧容器 ${BACKUP_CONTAINER} 未能删除"
    OLD_CONTAINER_MOVED=false
fi

DEPLOYMENT_COMPLETE=true
docker image rm "${CANDIDATE_IMAGE}" >/dev/null 2>&1 || true
if [ "${SOURCE_BACKED_UP}" = 'true' ]; then
    log "部署完成，源码备份位于 ${BACKUP_SOURCE_DIR}"
else
    log "首次部署完成，源码目录位于 ${DEPLOY_DIR}"
fi
log "健康检查地址：http://127.0.0.1:${HOST_PORT}/api/health"
