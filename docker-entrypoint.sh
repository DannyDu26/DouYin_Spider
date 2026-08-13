#!/bin/sh
set -eu

# 清理容器异常终止后可能遗留的 Xvfb 文件。
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix
rm -f /tmp/.X11-unix/X99 /tmp/.X99-lock

export DISPLAY=:99
echo "正在启动 Xvfb，DISPLAY=${DISPLAY}"
Xvfb "${DISPLAY}" -screen 0 1280x960x24 -nolisten tcp &
xvfb_pid=$!

# 等待虚拟显示就绪，失败时输出明确错误并终止容器。
attempt=0
while [ ! -S /tmp/.X11-unix/X99 ]; do
    if ! kill -0 "${xvfb_pid}" 2>/dev/null; then
        wait "${xvfb_pid}" || true
        echo "Xvfb 启动失败" >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    if [ "${attempt}" -ge 100 ]; then
        echo "等待 Xvfb 就绪超时" >&2
        exit 1
    fi
    sleep 0.1
done

echo "Xvfb 已就绪，正在启动 Uvicorn"
exec python -u -m uvicorn main:app \
    --host 0.0.0.0 \
    --port 5000 \
    --workers 1
