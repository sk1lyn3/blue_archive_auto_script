#!/bin/bash
# 将 BAAS WebUI 安装为 systemd 服务
# 用法: sudo bash deploy/install-service.sh [项目路径]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_DIR="${1:-$SCRIPT_DIR}"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
SERVICE_FILE="/etc/systemd/system/baas-webui.service"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "错误: 找不到 $VENV_PYTHON，请先创建 venv 并安装依赖"
    exit 1
fi

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=BAAS WebUI Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_PYTHON webui_server.py --host 0.0.0.0 --port 8765
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable baas-webui
systemctl restart baas-webui

echo "服务已启动，查看日志: journalctl -u baas-webui -f"
echo "停止服务: systemctl stop baas-webui"
