#!/bin/bash
set -euo pipefail

SSH_KEY="$HOME/.ssh/claude_key"
SSH_OPTS="-i $SSH_KEY -p 2223 -o StrictHostKeyChecking=no"
# Адрес сервера в deploy.local (gitignored), см. deploy.local.example.
# Вне дома можно переопределить: DATADOM_HOST=<tailscale-ip> ./deploy.sh
[ -f "$(dirname "$0")/deploy.local" ] && . "$(dirname "$0")/deploy.local"
SERVER="claude-bot@${DATADOM_HOST:?задай DATADOM_HOST или deploy.local}"
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
