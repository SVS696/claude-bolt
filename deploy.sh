#!/bin/bash
# Deploy bridge files to VPS and restart
set -euo pipefail

# Load secrets from .env
if [ -f "$(dirname "$0")/.env" ]; then
  set -a; source "$(dirname "$0")/.env"; set +a
fi

VPS="root@${VPS_HOST:?Set VPS_HOST}"
PORT="${VPS_PORT:-3333}"
PASS="${VPS_PASS:?Set VPS_PASS}"
REMOTE="/root/claude-telegram-bridge"
LOCAL="$(dirname "$0")/bridge"

echo "Deploying to VPS..."
sshpass -p "$PASS" scp -o StrictHostKeyChecking=no -P $PORT \
  "$LOCAL/bot.py" \
  "$LOCAL/claude_runner.py" \
  "$LOCAL/config.py" \
  "$LOCAL/usage_tracker.py" \
  "$LOCAL/cron_wrapper.sh" \
  "$LOCAL/CLAUDE.md" \
  "$VPS:$REMOTE/"

echo "Restarting bridge..."
sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no -p $PORT $VPS \
  "chmod +x $REMOTE/cron_wrapper.sh && cp $REMOTE/CLAUDE.md /root/.openclaw/workspace-main/.claude/CLAUDE.md && systemctl restart claude-telegram && sleep 2 && systemctl is-active claude-telegram"

echo "Done!"
