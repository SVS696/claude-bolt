#!/bin/bash
# Cron wrapper for Claude Code
# Usage: cron_wrapper.sh <cron-name> <prompt-file> [timeout]

set -euo pipefail

CRON_NAME="${1:-unnamed}"
PROMPT_FILE="${2:-}"
TIMEOUT="${3:-600}"
CHAT_ID="${TELEGRAM_CHAT_ID:?Set TELEGRAM_CHAT_ID in .env}"
BOT_TOKEN="${TELEGRAM_BOT_TOKEN}"
CLAUDE_BIN="/root/.local/share/fnm/node-versions/v24.13.1/installation/bin/claude"
WORKSPACE="/root/.openclaw/workspace-main"
LOG_DIR="/root/.claude-bridge/cron-logs"
MODELS_FILE="/root/claude-telegram-bridge/models.json"
export PATH="/root/.local/share/fnm/node-versions/v24.13.1/installation/bin:$PATH"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${CRON_NAME}-$(date +%Y%m%d-%H%M%S).log"

START_TIME=$(date +%s)
echo "$(date): Starting cron '$CRON_NAME'" >> "$LOG_FILE"

# Read prompt from file
if [ ! -f "$PROMPT_FILE" ]; then
    echo "$(date): Prompt file not found: $PROMPT_FILE" >> "$LOG_FILE"
    exit 1
fi
PROMPT=$(cat "$PROMPT_FILE")

# Determine model for this cron
MODEL=""
if [ -f "$MODELS_FILE" ]; then
    # Check per-cron override first
    CRON_MODEL=$(python3 -c "
import json
with open('$MODELS_FILE') as f:
    cfg = json.load(f)
m = cfg.get('crons', {}).get('$CRON_NAME', cfg.get('default', 'claude-sonnet-4-6'))
print(m)
" 2>/dev/null)
    if [ -n "$CRON_MODEL" ]; then
        MODEL="$CRON_MODEL"
    fi
fi

MODEL_FLAG=""
if [ -n "$MODEL" ]; then
    MODEL_FLAG="--model $MODEL"
fi

# Run Claude Code
cd "$WORKSPACE"
OUTPUT=$(timeout "$TIMEOUT" "$CLAUDE_BIN" \
    --print \
    --output-format text \
    $MODEL_FLAG \
    "$PROMPT" 2>>"$LOG_FILE" || echo "[Error: Claude Code exited with code $?]")

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
echo "$(date): Claude finished (model: ${MODEL:-default}), output length: ${#OUTPUT}, duration: ${DURATION}s" >> "$LOG_FILE"

# Record usage
python3 -c "
import sys
sys.path.insert(0, '/root/claude-telegram-bridge')
from usage_tracker import record_invocation
record_invocation('cron:$CRON_NAME', '${MODEL:-claude-sonnet-4-6}', $DURATION)
" 2>/dev/null

# Check for heartbeat/no-reply signals
if echo "$OUTPUT" | grep -qiE '^(HEARTBEAT_OK|NO_REPLY)$'; then
    echo "$(date): Heartbeat/no-reply signal, skipping Telegram send" >> "$LOG_FILE"
    exit 0
fi

# Skip empty output
if [ -z "$(echo "$OUTPUT" | tr -d '[:space:]')" ]; then
    echo "$(date): Empty output, skipping" >> "$LOG_FILE"
    exit 0
fi

# Send to Telegram
send_telegram() {
    local text="$1"
    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}" \
        --data-urlencode "text=${text}" \
        --max-time 30 >> "$LOG_FILE" 2>&1
}

# Split output if too long (4096 char limit)
MAX_LEN=4000
if [ ${#OUTPUT} -le $MAX_LEN ]; then
    send_telegram "$OUTPUT"
else
    CHUNK=""
    while IFS= read -r line; do
        if [ $(( ${#CHUNK} + ${#line} + 1 )) -gt $MAX_LEN ]; then
            [ -n "$CHUNK" ] && send_telegram "$CHUNK"
            CHUNK="$line"
        else
            CHUNK="${CHUNK}${CHUNK:+$'\n'}${line}"
        fi
    done <<< "$OUTPUT"
    [ -n "$CHUNK" ] && send_telegram "$CHUNK"
fi

echo "$(date): Sent to Telegram" >> "$LOG_FILE"

# Cleanup old logs (keep last 7 days)
find "$LOG_DIR" -name "*.log" -mtime +7 -delete 2>/dev/null
