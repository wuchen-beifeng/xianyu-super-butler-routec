#!/bin/sh
# 启动容器内 routec-solver（路线 C 求解服务，T7 整合进管家镜像）
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p /app/logs
export ROUTEC_LOG="/app/logs/routec-solver.log"
if [ -f /app/data/SLIDER_ROUTE_C_DISABLED ]; then
    echo "[routec] kill switch 存在，跳过启动"
    exit 0
fi
echo "[routec] 启动 solver :8799（driver/CDP -> VM100）"
exec python "$DIR/solver.py" --bind 127.0.0.1 --port 8799 --token-file "$DIR/token"
