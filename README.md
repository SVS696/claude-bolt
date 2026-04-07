# Claude Bolt

**Claude Code in your Telegram.**

Claude Bolt bridges the full [Claude Code CLI](https://github.com/anthropics/claude-code) to a Telegram bot, giving you a personal AI assistant that can execute bash commands, read and edit files, search the web, run scripts — all from a chat message. Not just an API wrapper: the real Claude Code tool suite, in your pocket.

> **Warning: Personal tool.** Claude Bolt was built for single-owner, self-hosted use on a VPS. It is battle-tested but opinionated — paths, cron prompts, and the workspace layout reflect a specific workflow. Treat this as a strong starting point, not a generic framework.

---

## What makes it different

| Feature | Claude Bolt | Typical Claude bot |
|---|---|---|
| Underlying engine | Claude Code CLI (tools enabled) | Anthropic API (text only) |
| Bash / file / web access | Yes — full tool suite | No |
| Session continuity | Yes — resumes across messages | Stateless per request |
| Streaming UI | Plan → Progress → Result | Single response |
| Scheduled tasks (crons) | Yes — 16+ built-in examples | No |
| Per-cron model selection | Yes | No |
| Self-improvement | Bot can propose its own code changes | No |
| Media handling | Photos, documents, voice, media groups | Varies |

---

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌──────────────────┐     ┌────────────────┐
│  Telegram   │────▶│   bot.py    │────▶│ claude_runner.py │────▶│ Claude Code    │
│  (user)     │◀────│  (handler)  │◀────│  (stream-json)   │◀────│ CLI + tools    │
└─────────────┘     └─────────────┘     └──────────────────┘     └────────────────┘
                                                                         │
                                                              ┌──────────┴──────────┐
                                                              │ Bash · Edit · Read  │
                                                              │ Web search · Write  │
                                                              │ Glob · Grep · ...   │
                                                              └─────────────────────┘

┌──────────┐     ┌────────────────────┐     ┌──────────────────┐     ┌─────────────┐
│ Crontab  │────▶│  cron_wrapper.sh   │────▶│ Claude Code CLI  │────▶│ Telegram    │
│          │     │  (model selection) │     │  (--print mode)  │     │ Bot API     │
└──────────┘     └────────────────────┘     └──────────────────┘     └─────────────┘
```

**Data flow for interactive messages:**

1. User sends a message (text, photo, document, voice, or a media group)
2. `bot.py` batches consecutive messages with a 1-second debounce, enriches them with timestamp and reply context
3. `claude_runner.py` launches `claude --print --output-format stream-json` and streams NDJSON events
4. Events are classified as `TEXT` (plan/result) or `ACTION` (tool use) and forwarded to Telegram in real time
5. Session ID is preserved so the next message continues the same conversation

**Data flow for cron tasks:**

1. `cron_wrapper.sh` reads the prompt file, resolves the per-cron model from `models.json`, runs `claude --print`
2. Output is posted to Telegram via Bot API; `HEARTBEAT_OK` / `NO_REPLY` signals suppress posting
3. Usage is recorded in `usage_tracker.py`

---

## Prerequisites

- **Linux VPS** with systemd (Ubuntu 22.04 / 24.04 recommended)
- **Python 3.10+**
- **Node.js** — install via [fnm](https://github.com/Schniz/fnm) (Claude Code requires a recent Node)
- **Claude Code CLI** — installed and authenticated (`claude auth login`)
- **Telegram bot token** — created via [@BotFather](https://t.me/BotFather)
- **sshpass** (optional) — only needed for the `deploy.sh` helper script

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/your-username/claude-bolt.git
cd claude-bolt
```

### 2. Install Python dependencies

```bash
pip install -r bridge/requirements.txt
```

### 3. Create and configure `.env`

```bash
cp bridge/.env.example bridge/.env
```

Edit `bridge/.env`:

```env
TELEGRAM_BOT_TOKEN=your-bot-token-here
TELEGRAM_OWNER_ID=your-telegram-user-id
CLAUDE_BIN=/path/to/fnm/node-versions/.../bin/claude
CLAUDE_WORKSPACE=/path/to/your/workspace
SESSION_DIR=/root/.claude-bridge/sessions
CRON_LOG_DIR=/root/.claude-bridge/cron-logs
```

### 4. Create a Telegram bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram
2. Send `/newbot` and follow the prompts
3. Copy the token into `TELEGRAM_BOT_TOKEN`
4. Find your numeric user ID (e.g. via [@userinfobot](https://t.me/userinfobot)) and set `TELEGRAM_OWNER_ID`

### 5. Install and authenticate Claude Code

```bash
# Install Node via fnm
curl -fsSL https://fnm.vercel.app/install | bash
fnm install --lts

# Install Claude Code
npm install -g @anthropic-ai/claude-code

# Authenticate
claude auth login
```

### 6. Prepare the workspace

Create a directory that Claude Code will use as its working directory:

```bash
mkdir -p /path/to/your/workspace/.claude
```

Copy `bridge/CLAUDE.md` into `.claude/CLAUDE.md` inside the workspace to set the bot's personality and instructions.

### 7. Set up the systemd service

```bash
cp bridge/systemd/claude-telegram.service /etc/systemd/system/
# Edit the service file to match your paths
systemctl daemon-reload
systemctl enable claude-telegram
systemctl start claude-telegram
systemctl status claude-telegram
```

### 8. Test it

Open your bot in Telegram and send a message. You should see the "Thinking..." indicator followed by a response.

---

## Configuration

### `.env` variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from BotFather |
| `TELEGRAM_OWNER_ID` | Yes | Your Telegram numeric user ID. All other users are rejected. |
| `CLAUDE_BIN` | No | Full path to the `claude` binary. Default: fnm-managed path. |
| `CLAUDE_WORKSPACE` | No | Working directory for Claude Code. Default: `/root/.openclaw/workspace-main` |
| `SESSION_DIR` | No | Directory to store session state. Default: `/root/.claude-bridge/sessions` |
| `CRON_LOG_DIR` | No | Directory for cron execution logs. Default: `/root/.claude-bridge/cron-logs` |
| `VPS_HOST` | deploy only | VPS hostname/IP for `deploy.sh` |
| `VPS_PORT` | deploy only | SSH port for `deploy.sh` |
| `VPS_PASS` | deploy only | SSH password for `deploy.sh` |

### `config.py`

`config.py` reads from environment variables. The key settings:

```python
TELEGRAM_MAX_MESSAGE_LENGTH = 4096   # Telegram hard limit
CLAUDE_WORKSPACE = ...               # Where Claude Code runs
FNM_BIN = ...                        # Added to PATH so Node is found
```

Adjust defaults here if your paths differ from the included examples.

### `models.json`

Controls which Claude model is used by default and per cron:

```json
{
  "default": "claude-sonnet-4-6",
  "crons": {
    "morning-brief": "claude-opus-4-6",
    "ai-channels-digest": "claude-sonnet-4-6"
  }
}
```

Change the default interactively with `/model sonnet` (or `opus`, `haiku`) in Telegram.  
Set a per-cron model with `/model <cron-name> <model>`.

### `CLAUDE.md` — bot personality

`bridge/CLAUDE.md` is the system prompt that defines the bot's identity, knowledge, communication style, and workspace layout. It is deployed to `<CLAUDE_WORKSPACE>/.claude/CLAUDE.md` by `deploy.sh`.

Customize it to match your own environment:
- Name, timezone, and language preferences
- Paths to your scripts, vaults, and skills
- Rules for cron output format (heartbeat signals, reply suppression)
- Self-improvement guidelines if you want the bot to propose code changes

### Claude Code permissions

Claude Code by default asks for permission before running tools. On a server running as root you can pre-approve all tools via `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": ["Bash(*)", "Edit(*)", "Write(*)", "Read(*)", "Glob(*)", "Grep(*)"]
  }
}
```

Adjust the allow list to your comfort level.

---

## Cron System

The cron system lets Claude Code execute scheduled tasks and push results to Telegram automatically.

### Structure

```
bridge/
├── cron-prompts/       # One .txt file per cron = the task description
│   ├── morning-brief.txt
│   ├── evening-summary.txt
│   └── ...
├── cron_wrapper.sh     # Runs claude, handles model selection, posts to Telegram
└── models.json         # Per-cron model overrides
```

### Creating a cron

**Step 1.** Write the prompt file:

```bash
cat > /path/to/bridge/cron-prompts/my-task.txt << 'EOF'
Check today's calendar and summarize upcoming deadlines.
If nothing is scheduled, reply HEARTBEAT_OK.
EOF
```

**Step 2.** Add the crontab entry:

```bash
# Add to system crontab:
# 30 9 * * 1-5  = weekdays at 09:30
30 9 * * 1-5 /path/to/bridge/cron_wrapper.sh "my-task" "/path/to/bridge/cron-prompts/my-task.txt" 300
```

`cron_wrapper.sh` arguments: `<cron-name> <prompt-file-path> [timeout-seconds]`

**Step 3.** Optionally set a specific model for this cron:

```bash
/model my-task haiku
```

Or directly in `models.json`:

```json
{
  "crons": {
    "my-task": "claude-haiku-4-5"
  }
}
```

### Heartbeat signals

If the cron has nothing to report, the prompt should instruct Claude to reply with exactly:
- `HEARTBEAT_OK` — task ran, nothing to report
- `NO_REPLY` — same effect

The wrapper will not send these strings to Telegram, keeping your chat clean.

### Bundled cron examples

The `bridge/cron-prompts/` directory includes 16+ real-world examples:
- `morning-brief.txt` — weather, calendar, open tasks
- `evening-summary.txt` — daily recap
- `ai-channels-digest.txt` — AI news digest from Telegram channels
- `weekly-health-report.txt` — health metrics summary
- `financial-checkup.txt` — portfolio and budget snapshot
- and more

Review and adapt them — they reference specific scripts and tools that you will need to replace with your own.

---

## Streaming UX

The bot shows three distinct phases while Claude works:

**Phase 1 — Thinking** (before Claude produces output):
```
⏳ Думаю... (4s)
```
An animated indicator updates every 2 seconds.

**Phase 2 — Plan** (first TEXT block, before any tool use):
```
I'll check the calendar and cross-reference with your task list...
```
Shown inline in the original reply, updated as text streams in.

**Phase 3 — Progress** (during tool use):
```
⚙️ Working... (12s)
→ $ python3 ~/.openclaw/scripts/yandex-calendar.py events
→ $ cd ~/.claude/skills/singularity-app && python3 cli.py ...
```
A separate message shows the last 3 tool actions, updating every second.

**Final result:**
- The progress message is deleted
- If a plan was shown: result arrives as a new message
- If no plan (pure tool → result): result replaces the "Thinking..." message
- Long responses are split at newline boundaries respecting the 4096-char limit

---

## Message Handling

### Batching

Consecutive messages within 1 second are merged into a single prompt. This handles the common pattern of sending a thought in two messages:

```
"Check my schedule for tomorrow"
"and also the weather"
→ sent as one prompt to Claude
```

### Reply / quote context

When you reply to a previous message in Telegram, the quoted content is extracted and prepended to the prompt so Claude sees the context.

### Media groups

If you send multiple photos or files at once (Telegram album), they are collected and processed as a single prompt listing all file paths. Claude can then analyze or describe all of them together.

### File handling

- **Photos** — downloaded to `/tmp/tg_files/` and passed as file paths
- **Documents** — downloaded to `/tmp/tg_files/<filename>` and passed as file paths
- **Voice messages** — downloaded and passed; if a Whisper transcription script exists, it is invoked automatically

---

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Show help |
| `/new` | Start a new conversation session |
| `/stop` | Cancel the current running task |
| `/status` | Show session state, active model, cron count, usage link |
| `/crons` | List all crons with their schedule and assigned model |
| `/model` | Show or change the default model |
| `/model <name>` | Switch default to `opus`, `sonnet`, or `haiku` |
| `/model <cron> <name>` | Set per-cron model override |

---

## Usage Tracking

Every invocation (interactive and cron) is recorded in `~/.claude-bridge/usage.json` with:
- Timestamp and source (chat vs cron name)
- Model used
- Input / output / cache tokens
- Cost in USD
- Duration
- Context window size

Records older than 7 days are automatically pruned. The `/status` command shows a 24h / 7d summary. Detailed billing is at `claude.ai/settings/usage`.

---

## Deploying to VPS

### First deploy

```bash
# On VPS: create the bridge directory
mkdir -p /root/claude-telegram-bridge
cd /root/claude-telegram-bridge

# Copy bridge files
scp -P <port> bridge/* root@<vps-ip>:/root/claude-telegram-bridge/

# Create .env with secrets
nano /root/claude-telegram-bridge/.env

# Install service
cp /root/claude-telegram-bridge/systemd/claude-telegram.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now claude-telegram
```

### Subsequent deploys

The included `deploy.sh` script handles SCP + service restart:

```bash
# Set VPS_HOST, VPS_PORT, VPS_PASS in .env first
./deploy.sh
```

`deploy.sh` copies `bot.py`, `claude_runner.py`, `config.py`, `usage_tracker.py`, `cron_wrapper.sh`, and `CLAUDE.md`, then restarts the service and reports its status.

### Monitoring

```bash
# Live logs
journalctl -u claude-telegram -f

# Last 50 lines
journalctl -u claude-telegram -n 50 --no-pager

# Cron logs
ls -lt /root/.claude-bridge/cron-logs/ | head -20
cat /root/.claude-bridge/cron-logs/<cron-name>-*.log
```

### Disk management

Cron logs are auto-pruned after 7 days. Usage records are trimmed to 7 days. Temporary media files in `/tmp/tg_files/` are not auto-cleaned — add a cron to purge them if needed.

---

## Customization

### Making it yours

1. **Edit `bridge/CLAUDE.md`** — this is the most important file. It defines who the bot is, what it knows about you, and what tools and scripts are available. Replace all workspace paths, script references, and personal details.

2. **Write your own cron prompts** — the included examples are real but personal. Delete them and write prompts that fit your actual workflows (daily reports, health tracking, finance, etc.).

3. **Adjust the workspace** — `CLAUDE_WORKSPACE` is where Claude Code runs its tools. Put your scripts, data files, and memory directory here.

4. **Set up Claude Code settings** — `~/.claude/settings.json` controls which tools are pre-approved. Tune this to your risk tolerance.

5. **Model selection** — use `haiku` for lightweight crons, `sonnet` for general use, `opus` for complex reasoning tasks. Set defaults in `models.json`.

### Self-improvement

The bot includes instructions (in `CLAUDE.md`) for how it should propose changes to its own code:

1. Make edits to the bridge files in-place
2. Commit to a dedicated branch
3. Notify the owner via Telegram
4. Owner reviews and merges

This is entirely optional. Remove the self-improvement section from `CLAUDE.md` if you prefer the bot not to touch its own code.

---

## Migration from OpenClaw

Claude Bolt replaces the OpenClaw Telegram bridge with a cleaner architecture:

- **No more OpenClaw daemon** — `bot.py` runs as a straightforward systemd service
- **Stream-JSON output** instead of polling or scraping — real-time streaming via the official `--output-format stream-json` flag
- **Explicit session management** — `--resume <session-id>` instead of implicit session state
- **Cron prompts as plain text files** — no database, no config format to learn
- **Per-cron model selection** — `models.json` replaces hardcoded model choices
- **Unified usage tracking** — single JSON file covers both chat and cron invocations

Existing cron prompts from OpenClaw can be copied as-is into `bridge/cron-prompts/`. The `cron_wrapper.sh` call signature is:
```
cron_wrapper.sh "<name>" "<prompt-file>" [timeout]
```

---

## Security Notes

- **Secrets belong in `.env`**, never in source code. `config.py` reads everything from environment variables.
- **`.env` is in `.gitignore`** — verify this before pushing to any remote.
- **Owner-only access** — `bot.py` rejects all messages from users who are not `TELEGRAM_OWNER_ID`. This is enforced at the handler level via the `@owner_only` decorator.
- **Claude Code permissions** — pre-approving all tools (`settings.json`) is convenient but means Claude can run arbitrary bash commands. Run on a dedicated VPS, not a machine with sensitive local data.
- **OAuth tokens** — Claude Code stores OAuth credentials in `~/.claude/.credentials.json`. Back these up; if the service restarts after token expiry you will need to run `claude auth login` on the VPS again.
- **SSH access** — if you use `deploy.sh`, store VPS credentials in `.env`, not in the script itself.

---

## Project Structure

```
claude-bolt/
├── bridge/
│   ├── bot.py                  # Telegram bot: handlers, streaming, formatting
│   ├── claude_runner.py        # Claude Code CLI runner, stream-json parser
│   ├── config.py               # Configuration (reads from .env)
│   ├── usage_tracker.py        # Per-invocation usage recording
│   ├── cron_wrapper.sh         # Cron task runner
│   ├── CLAUDE.md               # Bot personality and system prompt
│   ├── models.json             # Default and per-cron model config
│   ├── requirements.txt        # Python dependencies
│   ├── cron-prompts/           # One .txt file per scheduled task
│   └── systemd/
│       └── claude-telegram.service
└── deploy.sh                   # Deploy to VPS and restart service
```

---

## License

TODO

---

Built on [Claude Code](https://github.com/anthropics/claude-code) by Anthropic.
