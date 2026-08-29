#!/bin/bash
set -euo pipefail

SSH_KEY="$HOME/.ssh/claude_key"
SSH_OPTS="-i $SSH_KEY -p 2223 -o StrictHostKeyChecking=no"
# LAN по умолчанию; вне дома: DATADOM_HOST=<tailscale-ip> (tailscale) ./deploy.sh
SERVER="claude-bot@${DATADOM_HOST:-<lan-ip>}"
REMOTE_DIR="summarizer_bot"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Загружаю файлы на сервер..."
rsync -avz -e "ssh $SSH_OPTS" \
  --exclude='__pycache__' \
  --exclude='.venv' \
  --exclude='*.pyc' \
  "$SCRIPT_DIR/bot.py" \
  "$SCRIPT_DIR/config.py" \
  "$SCRIPT_DIR/extractors.py" \
  "$SCRIPT_DIR/summarizer.py" \
  "$SCRIPT_DIR/requirements.txt" \
  "$SCRIPT_DIR/Dockerfile" \
  "$SCRIPT_DIR/docker-compose.yml" \
  "$SCRIPT_DIR/.env" \
  "$SERVER:$REMOTE_DIR/"

echo "==> Собираю и запускаю контейнер..."
ssh $SSH_OPTS "$SERVER" "cd $REMOTE_DIR && docker-compose up -d --build"

echo "==> Готово! Проверяю статус..."
ssh $SSH_OPTS "$SERVER" "cd $REMOTE_DIR && docker-compose ps"
