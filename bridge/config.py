"""Configuration for Claude Code Telegram Bridge."""
import os

# Telegram
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"])
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# Claude Code
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/root/.local/share/fnm/node-versions/v24.13.1/installation/bin/claude")
CLAUDE_WORKSPACE = os.environ.get("CLAUDE_WORKSPACE", "/root/.openclaw/workspace-main")
CLAUDE_FLAGS = [
    "--dangerously-skip-permissions",
    "--output-format", "stream-json",
    "--verbose",
]

# Node/fnm PATH
FNM_BIN = "/root/.local/share/fnm/node-versions/v24.13.1/installation/bin"

# Sessions
SESSION_DIR = os.environ.get("SESSION_DIR", "/root/.claude-bridge/sessions")

# Cron output
CRON_LOG_DIR = os.environ.get("CRON_LOG_DIR", "/root/.claude-bridge/cron-logs")
