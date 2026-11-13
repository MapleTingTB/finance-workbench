#!/usr/bin/env bash
# 理财工作台 · 本机预览服务（PWA 需要 http://localhost 或 HTTPS 才能注册 Service Worker）
# 用法： ./start.sh [端口]   默认 8080
set -e
cd "$(dirname "$0")"
PORT="${1:-8080}"
echo "=========================================================="
echo "  理财工作台 · 本机预览"
echo "  本机浏览器：  http://localhost:${PORT}/"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -n "$IP" ]; then
  echo "  同网段手机：  http://${IP}:${PORT}/   （鸿蒙浏览器可直接打开）"
fi
echo "  提示：真机『添加到桌面』需 HTTPS；原生 .hap 请用 harmony/ 工程"
echo "  按 Ctrl+C 停止"
echo "=========================================================="
python3 -m http.server "$PORT" --bind 0.0.0.0
