#!/bin/bash
# Setup script for Claude Code Telegram Bridge on VPS
set -euo pipefail

echo "=== Claude Code Telegram Bridge Setup ==="

export PATH="/root/.local/share/fnm/node-versions/v24.13.1/installation/bin:$PATH"

# 1. Create directories
echo "[1/7] Creating directories..."
mkdir -p /root/.claude-bridge/{sessions,cron-logs}
mkdir -p /root/claude-telegram-bridge

# 2. Copy bridge files
echo "[2/7] Bridge files already in place..."

# 3. Install Python dependencies
echo "[3/7] Installing Python dependencies..."
pip3 install python-telegram-bot --break-system-packages -q

# 4. Setup CLAUDE.md for workspace
echo "[4/7] Setting up CLAUDE.md..."
if [ ! -f /root/.openclaw/workspace-main/.claude/CLAUDE.md ]; then
    mkdir -p /root/.openclaw/workspace-main/.claude
fi

# 5. Make cron wrapper executable
echo "[5/7] Setting permissions..."
chmod +x /root/claude-telegram-bridge/cron_wrapper.sh
chmod +x /root/claude-telegram-bridge/bot.py

# 6. Install systemd service
echo "[6/7] Installing systemd service..."
cp /root/claude-telegram-bridge/systemd/claude-telegram.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable claude-telegram.service

# 7. Verify Claude Code
echo "[7/7] Verifying Claude Code..."
claude --version

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Run: claude login"
echo "  2. Run: python3 /root/claude-telegram-bridge/migrate_crons.py"
echo "  3. Review: cat /root/claude-telegram-bridge/generated-crontab"
echo "  4. Install crons: crontab /root/claude-telegram-bridge/generated-crontab"
echo "  5. Start bridge: systemctl start claude-telegram"
echo "  6. Stop OpenClaw: systemctl stop openclaw-gateway (or kill the process)"
