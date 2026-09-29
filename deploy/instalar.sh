#!/usr/bin/env bash
# Instala el bot en un servidor Ubuntu y lo deja corriendo como servicio.
# Uso (desde la carpeta del bot):  bash deploy/instalar.sh
set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USUARIO="$(whoami)"
SERVICIO="bot-analisis"

echo "==> Carpeta del bot: $BOT_DIR (usuario: $USUARIO)"

if [ ! -f "$BOT_DIR/.env" ]; then
    echo "ERROR: falta el archivo .env en $BOT_DIR" >&2
    exit 1
fi

echo "==> Instalando Python..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

echo "==> Creando entorno virtual e instalando librerías..."
python3 -m venv "$BOT_DIR/.venv"
"$BOT_DIR/.venv/bin/pip" install --upgrade pip
"$BOT_DIR/.venv/bin/pip" install -r "$BOT_DIR/requirements.txt"

echo "==> Creando servicio $SERVICIO..."
sudo tee "/etc/systemd/system/$SERVICIO.service" > /dev/null <<EOF
[Unit]
Description=Bot de Discord ANALISIS (partidas de LoL)
After=network-online.target
Wants=network-online.target

[Service]
User=$USUARIO
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/.venv/bin/python -u bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICIO"
sudo systemctl restart "$SERVICIO"

sleep 5
sudo systemctl --no-pager status "$SERVICIO" || true
echo
echo "==> Listo. Ver el log en vivo con:  journalctl -u $SERVICIO -f"
