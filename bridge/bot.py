#!/usr/bin/env python3
"""Claude Code Telegram Bridge - main bot."""
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from telegram import Update, BotCommand
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, TELEGRAM_MAX_MESSAGE_LENGTH,
    CLAUDE_BIN, FNM_BIN, CLAUDE_WORKSPACE,
)
from claude_runner import ClaudeRunner
from usage_tracker import record_invocation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/root/.claude-bridge/bridge.log"),
    ],
)
logger = logging.getLogger("bridge")

runner = ClaudeRunner()
_active_tasks: dict[int, asyncio.Task] = {}

# ─── Message context helpers ───

_media_groups: dict[str, dict] = {}  # media_group_id → {messages: [], timer: Task}
_msg_batches: dict[int, dict] = {}  # chat_id → {parts: [], timer: Task, update, context}
MEDIA_GROUP_WAIT = 0.5  # seconds to wait for more messages in a group
MSG_BATCH_WAIT = 1.0  # seconds to wait for consecutive text messages


def _format_timestamp(msg) -> str:
    """Format message timestamp in Moscow time."""
    from datetime import timezone, timedelta
    msk = timezone(timedelta(hours=3))
    dt = msg.date.astimezone(msk)
    return dt.strftime("%Y-%m-%d %H:%M:%S MSK")


def _extract_reply_context(msg) -> str:
    """Extract quoted/replied message context."""
    reply = msg.reply_to_message
    if not reply:
        return ""

    parts = []
    sender = reply.from_user
    sender_name = sender.full_name if sender else "Unknown"

    from datetime import timezone, timedelta
    msk = timezone(timedelta(hours=3))
    reply_time = reply.date.astimezone(msk).strftime("%H:%M:%S")

    parts.append(f"[Цитата сообщения от {sender_name} ({reply_time})]")

    if reply.text:
        parts.append(reply.text)
    elif reply.caption:
        parts.append(f"[Медиа с подписью: {reply.caption}]")
    elif reply.photo:
        parts.append("[Фото]")
    elif reply.document:
        parts.append(f"[Файл: {reply.document.file_name}]")
    elif reply.voice:
        parts.append("[Голосовое сообщение]")
    else:
        parts.append("[Сообщение без текста]")

    parts.append("[/Цитата]")
    return "\n".join(parts)


def _extract_forward_info(msg) -> str:
    """Extract forward source info."""
    origin = getattr(msg, 'forward_origin', None)
    if origin:
        if hasattr(origin, 'sender_user') and origin.sender_user:
            return f"[Переслано от {origin.sender_user.full_name}]"
        elif hasattr(origin, 'chat') and origin.chat:
            return f"[Переслано из {origin.chat.title or origin.chat.username or 'чата'}]"
        elif hasattr(origin, 'sender_user_name') and origin.sender_user_name:
            return f"[Переслано от {origin.sender_user_name}]"
        return "[Переслано]"
    return ""


def _build_prompt_parts(msg) -> list[str]:
    """Build context-enriched prompt parts from a message."""
    parts = []

    # Timestamp
    parts.append(f"[{_format_timestamp(msg)}]")

    # Reply context
    reply_ctx = _extract_reply_context(msg)
    if reply_ctx:
        parts.append(reply_ctx)

    # Forward info
    fwd = _extract_forward_info(msg)
    if fwd:
        parts.append(fwd)

    return parts


MODELS_FILE = "/root/claude-telegram-bridge/models.json"
CRON_PROMPTS_DIR = "/root/claude-telegram-bridge/cron-prompts"

AVAILABLE_MODELS = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}
DEFAULT_MODEL = "claude-sonnet-4-6"


def load_models_config() -> dict:
    try:
        with open(MODELS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"default": DEFAULT_MODEL, "crons": {}}


def save_models_config(config: dict):
    with open(MODELS_FILE, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != TELEGRAM_OWNER_ID:
            await update.message.reply_text("Access denied.")
            return
        return await func(update, context)
    return wrapper


def md_to_tg(text: str) -> str:
    """Convert Claude's markdown to Telegram HTML format.

    Telegram supported tags: <b>, <i>, <u>, <s>, <code>, <pre>, <a>, <tg-spoiler>
    Rule: <pre>/<code> cannot contain other formatting tags.
    """
    # Step 1: Escape HTML entities FIRST
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")

    # Step 2: Extract code blocks and inline code to protect from formatting
    placeholders = []

    def save_code_block(m):
        lang = m.group(1) or ""
        code = m.group(2)
        if lang:
            html = f'<pre><code class="language-{lang}">{code}</code></pre>'
        else:
            html = f"<pre>{code}</pre>"
        placeholders.append(html)
        return f"\x00CODE{len(placeholders) - 1}\x00"

    def save_inline_code(m):
        html = f"<code>{m.group(1)}</code>"
        placeholders.append(html)
        return f"\x00CODE{len(placeholders) - 1}\x00"

    text = re.sub(r"```(\w*)\n(.*?)```", save_code_block, text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", save_inline_code, text)

    # Step 3: Remove markdown escape backslashes BEFORE formatting
    text = re.sub(r"\\([.!?\-(){}[\]#*_~`>|])", r"\1", text)

    # Step 4: Bold **text** → <b>text</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # Step 5: Single *text* → bold (Claude uses * for emphasis)
    text = re.sub(r"(?<!\w)\*([^*\n]+?)\*(?!\w)", r"<b>\1</b>", text)

    # Step 6: Italic _text_ → <i>text</i>
    text = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", text)

    # Step 7: Strikethrough ~~text~~ → <s>text</s>
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # Step 8: Headers # text → bold (Telegram has no headers)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # Step 9: Links [text](url) → <a href="url">text</a>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    # Step 10: Blockquotes > text (Telegram supports blockquote since API 7.x)
    text = re.sub(r"^&gt;\s?(.+)$", r"<blockquote>\1</blockquote>", text, flags=re.MULTILINE)
    # Merge consecutive blockquotes
    text = re.sub(r"</blockquote>\n<blockquote>", "\n", text)

    # Step 11: Horizontal rules --- → just a line
    text = re.sub(r"^-{3,}$", "———", text, flags=re.MULTILINE)

    # Step 12: Restore code blocks
    for i, html in enumerate(placeholders):
        text = text.replace(f"\x00CODE{i}\x00", html)

    return text


async def safe_edit(msg, text: str, use_html: bool = True):
    """Edit message with HTML formatting, fallback to plain."""
    try:
        if use_html:
            await msg.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
        else:
            await msg.edit_text(text)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return  # Same content, not an error
        logger.warning(f"HTML edit failed: {e}\n--- HTML was ---\n{text[:500]}\n--- end ---")
        try:
            clean = re.sub(r"<[^>]+>", "", text)
            await msg.edit_text(clean)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"Edit failed: {e}")
        try:
            clean = re.sub(r"<[^>]+>", "", text)
            await msg.edit_text(clean)
        except Exception:
            pass


async def safe_send(bot, chat_id: int, text: str, use_html: bool = True):
    """Send message with HTML formatting, fallback to plain."""
    try:
        if use_html:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
        else:
            await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.warning(f"HTML send failed: {e}\n--- HTML was ---\n{text[:500]}\n--- end ---")
        try:
            clean = re.sub(r"<[^>]+>", "", text)
            await bot.send_message(chat_id=chat_id, text=clean)
        except Exception as e2:
            logger.error(f"Send failed: {e2}")


def split_message(text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def get_auth_info() -> dict:
    env = os.environ.copy()
    env["PATH"] = f"{FNM_BIN}:{env.get('PATH', '')}"
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "auth", "status"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        return json.loads(result.stdout or result.stderr)
    except Exception:
        return {}


def get_claude_version() -> str:
    env = os.environ.copy()
    env["PATH"] = f"{FNM_BIN}:{env.get('PATH', '')}"
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "--version"],
            capture_output=True, text=True, timeout=5, env=env,
        )
        return result.stdout.strip().split("\n")[0].replace(" (Claude Code)", "")
    except Exception:
        return "?"


def model_short(model: str) -> str:
    return next((k for k, v in AVAILABLE_MODELS.items() if v == model), model)


# ─── Commands ───

@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ Bolt 2.0\n\n"
        "/new — новая сессия\n"
        "/stop — отменить текущее\n"
        "/status — статус и usage\n"
        "/crons — список кронов\n"
        "/model — управление моделями\n\n"
        "Просто пиши — отвечу."
    )


@owner_only
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    runner.new_session(update.effective_chat.id)
    await update.message.reply_text("Новая сессия.")


@owner_only
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = _active_tasks.get(update.effective_chat.id)
    if task and not task.done():
        task.cancel()
        await update.message.reply_text("Отменено.")
    else:
        await update.message.reply_text("Ничего не запущено.")


@owner_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = runner.get_session(chat_id)
    task = _active_tasks.get(chat_id)
    running = task and not task.done()

    auth = get_auth_info()
    plan = auth.get("subscriptionType", "?")
    email = auth.get("email", "?")
    version = get_claude_version()

    models_cfg = load_models_config()
    cur_model = model_short(models_cfg.get("default", DEFAULT_MODEL))

    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        active_crons = sum(1 for l in result.stdout.splitlines()
                          if l.strip() and not l.strip().startswith("#") and "cron_wrapper" in l)
    except Exception:
        active_crons = "?"

    text = (
        f"⚡ Bolt 2.0 (Claude Code {version})\n"
        f"🧠 {cur_model} · {plan}\n"
        f"🔄 Session: {session.session_id[:8]}... · {'▶ running' if running else 'idle'}\n"
        f"⏰ Crons: {active_crons} active\n"
        f"\n"
        f"📊 claude.ai/settings/usage"
    )
    await update.message.reply_text(text)


@owner_only
async def cmd_crons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    models_cfg = load_models_config()
    cron_models = models_cfg.get("crons", {})
    default = models_cfg.get("default", DEFAULT_MODEL)

    prompts = sorted(Path(CRON_PROMPTS_DIR).glob("*.txt"))
    if not prompts:
        await update.message.reply_text("Кронов нет.")
        return

    schedules = {}
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            line = line.strip()
            if "cron_wrapper" not in line:
                continue
            m = re.search(r'cron_wrapper\.sh\s+"([^"]+)"', line)
            if m:
                name = m.group(1)
                disabled = line.startswith("#")
                parts = line.lstrip("# ").split()
                sched = " ".join(parts[:5])
                schedules[name] = (sched, disabled)
    except Exception:
        pass

    lines = [f"Default: {model_short(default)}\n"]
    for p in prompts:
        name = p.stem
        sched, disabled = schedules.get(name, ("?", False))
        m = model_short(cron_models.get(name, default))
        flag = "⏸" if disabled else "✅"
        lines.append(f"{flag} {name}\n   {sched} · {m}")

    for chunk in split_message("\n".join(lines)):
        await update.message.reply_text(chunk)


@owner_only
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    models_cfg = load_models_config()

    if not args:
        default = model_short(models_cfg.get("default", DEFAULT_MODEL))
        lines = [f"Default: {default}", ""]
        cron_models = models_cfg.get("crons", {})
        if cron_models:
            lines.append("Per-cron:")
            for cron, m in sorted(cron_models.items()):
                lines.append(f"  {cron}: {model_short(m)}")
        lines.append(f"\nДоступные: {', '.join(AVAILABLE_MODELS.keys())}")
        lines.append("/model <model> — сменить default")
        lines.append("/model <cron> <model> — для крона")
        await update.message.reply_text("\n".join(lines))
        return

    if len(args) == 1:
        key = args[0].lower()
        if key not in AVAILABLE_MODELS:
            await update.message.reply_text(f"Нет такой. Доступные: {', '.join(AVAILABLE_MODELS.keys())}")
            return
        models_cfg["default"] = AVAILABLE_MODELS[key]
        save_models_config(models_cfg)
        runner.set_model(AVAILABLE_MODELS[key])
        await update.message.reply_text(f"Default → {key}")
    elif len(args) == 2:
        cron, key = args[0], args[1].lower()
        if key == "default":
            models_cfg.setdefault("crons", {}).pop(cron, None)
            save_models_config(models_cfg)
            await update.message.reply_text(f"{cron} → default")
        elif key in AVAILABLE_MODELS:
            models_cfg.setdefault("crons", {})[cron] = AVAILABLE_MODELS[key]
            save_models_config(models_cfg)
            await update.message.reply_text(f"{cron} → {key}")
        else:
            await update.message.reply_text(f"Нет такой. Доступные: {', '.join(AVAILABLE_MODELS.keys())}")


# ─── Message handling with streaming ───

@owner_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = update.message
    if not msg.text and not msg.caption:
        return

    # Media group handling
    if msg.media_group_id:
        await _collect_media_group(update, context, msg)
        return

    # Batch consecutive messages with debounce
    msg_parts = _build_prompt_parts(msg)
    msg_parts.append(msg.text or msg.caption or "")
    msg_text = "\n".join(msg_parts)

    if chat_id not in _msg_batches:
        _msg_batches[chat_id] = {
            "parts": [],
            "update": update,
            "context": context,
            "timer": None,
        }

    _msg_batches[chat_id]["parts"].append(msg_text)
    _msg_batches[chat_id]["update"] = update  # keep latest for reply

    # Cancel previous timer, set new one
    timer = _msg_batches[chat_id].get("timer")
    if timer and not timer.done():
        timer.cancel()

    _msg_batches[chat_id]["timer"] = asyncio.create_task(
        _flush_msg_batch(chat_id)
    )


async def _flush_msg_batch(chat_id: int):
    """Wait for batch to settle, then process all messages as one prompt."""
    await asyncio.sleep(MSG_BATCH_WAIT)

    batch = _msg_batches.pop(chat_id, None)
    if not batch:
        return

    parts = batch["parts"]
    update = batch["update"]
    context = batch["context"]

    if len(parts) == 1:
        prompt = parts[0]
    else:
        prompt = f"[Группа из {len(parts)} последовательных сообщений]\n"
        prompt += "\n---\n".join(parts)

    prev = _active_tasks.get(chat_id)
    if prev and not prev.done():
        prev.cancel()
        await asyncio.sleep(0.3)

    task = asyncio.create_task(_process_message(update, context, prompt))
    _active_tasks[chat_id] = task


async def _collect_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE, msg):
    """Collect messages from a media group and process them together."""
    group_id = msg.media_group_id
    chat_id = update.effective_chat.id

    if group_id not in _media_groups:
        _media_groups[group_id] = {"messages": [], "update": update, "context": context}

    # Build this message's contribution
    entry_parts = []
    fwd = _extract_forward_info(msg)
    if fwd:
        entry_parts.append(fwd)

    if msg.text:
        entry_parts.append(msg.text)
    elif msg.caption:
        entry_parts.append(msg.caption)

    # Handle attached media
    if msg.photo:
        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        path = f"/tmp/tg_files/photo_{photo.file_id}.jpg"
        os.makedirs("/tmp/tg_files", exist_ok=True)
        await file.download_to_drive(path)
        entry_parts.append(f"[Фото: {path}]")
    elif msg.document:
        doc = msg.document
        file = await context.bot.get_file(doc.file_id)
        path = f"/tmp/tg_files/{doc.file_name}"
        os.makedirs("/tmp/tg_files", exist_ok=True)
        await file.download_to_drive(path)
        entry_parts.append(f"[Файл: {path} (имя: {doc.file_name})]")

    _media_groups[group_id]["messages"].append({
        "timestamp": _format_timestamp(msg),
        "parts": entry_parts,
    })

    # Cancel previous timer for this group, set new one
    timer = _media_groups[group_id].get("timer")
    if timer and not timer.done():
        timer.cancel()

    _media_groups[group_id]["timer"] = asyncio.create_task(
        _flush_media_group(group_id, chat_id, update, context)
    )


async def _flush_media_group(group_id: str, chat_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wait for all messages in group, then process as one."""
    await asyncio.sleep(MEDIA_GROUP_WAIT)

    group = _media_groups.pop(group_id, None)
    if not group:
        return

    msgs = group["messages"]
    first_msg = msgs[0]

    # Build combined prompt
    parts = [f"[{first_msg['timestamp']}]"]

    # Reply context from the first message in group
    reply_ctx = _extract_reply_context(group["update"].message)
    if reply_ctx:
        parts.append(reply_ctx)

    parts.append(f"[Группа из {len(msgs)} сообщений]")
    for i, m in enumerate(msgs, 1):
        entry = "\n".join(m["parts"])
        if entry.strip():
            parts.append(f"--- Сообщение {i} ---\n{entry}")

    prompt = "\n".join(parts)

    prev = _active_tasks.get(chat_id)
    if prev and not prev.done():
        prev.cancel()
        await asyncio.sleep(0.3)

    task = asyncio.create_task(_process_message(update, context, prompt))
    _active_tasks[chat_id] = task


async def _animate_thinking(reply, bot, chat_id: int, stop_event: asyncio.Event):
    """Animate the thinking indicator while Claude works."""
    frames = ["⏳ Думаю", "⏳ Думаю.", "⏳ Думаю..", "⏳ Думаю..."]
    i = 0
    while not stop_event.is_set():
        await asyncio.sleep(2)
        if stop_event.is_set():
            break
        i += 1
        elapsed = i * 2
        frame = frames[i % len(frames)]
        try:
            await reply.edit_text(f"{frame} ({elapsed}s)")
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass


async def _process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str):
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    reply = await update.message.reply_text("⏳ Думаю...")

    # Start thinking animation
    stop_anim = asyncio.Event()
    anim_task = asyncio.create_task(
        _animate_thinking(reply, context.bot, chat_id, stop_anim)
    )

    accumulated = ""
    got_first_chunk = False
    last_edit_text = ""
    last_edit_time = 0
    start_time = asyncio.get_event_loop().time()

    try:
        actions = []  # Tool actions
        plan_texts = []  # TEXT blocks before first ACTION (plan)
        result_texts = []  # TEXT blocks after first ACTION (result)
        had_actions = False  # Whether any ACTION was seen
        progress_msg = None  # Separate message for progress
        async for chunk in runner.run(message_text, chat_id):
            if chunk.startswith("\x01TEXT:"):
                text_chunk = chunk[6:].strip()
                if not text_chunk:
                    continue

                # Stop animation
                if not got_first_chunk:
                    got_first_chunk = True
                    stop_anim.set()
                    anim_task.cancel()
                    try:
                        await anim_task
                    except asyncio.CancelledError:
                        pass

                if not had_actions:
                    # Before any tool use → plan/thinking
                    plan_texts.append(text_chunk)
                    now = asyncio.get_event_loop().time()
                    if now - last_edit_time >= 0.5:
                        display = "\n\n".join(plan_texts)
                        if len(display) > TELEGRAM_MAX_MESSAGE_LENGTH - 50:
                            display = "..." + display[-(TELEGRAM_MAX_MESSAGE_LENGTH - 53):]
                        await safe_edit(reply, md_to_tg(display))
                        last_edit_time = now
                else:
                    # After tool use → result
                    result_texts.append(text_chunk)

            elif chunk.startswith("\x01ACTION:"):
                had_actions = True
                action = chunk[8:]
                actions.append(action)
                if len(actions) > 3:
                    actions = actions[-3:]

                # Stop animation
                if not got_first_chunk:
                    got_first_chunk = True
                    stop_anim.set()
                    anim_task.cancel()
                    try:
                        await anim_task
                    except asyncio.CancelledError:
                        pass

                # Show progress — always in separate message
                now = asyncio.get_event_loop().time()
                if now - last_edit_time >= 1.0:
                    elapsed = int(now - start_time)
                    display = f"⚙️ Работаю... ({elapsed}s)\n\n" + "\n".join(f"→ {a}" for a in actions)

                    if progress_msg is None:
                        try:
                            progress_msg = await context.bot.send_message(
                                chat_id=chat_id, text=display
                            )
                        except Exception:
                            pass
                    else:
                        try:
                            await progress_msg.edit_text(display)
                        except Exception:
                            pass
                    last_edit_time = now

            elif chunk.startswith("\x01DONE:"):
                pass  # Just a signal, stats already captured in runner

            else:
                pass  # Unexpected chunk, ignore

        # Stop animation if still going
        stop_anim.set()
        if not anim_task.done():
            anim_task.cancel()

        # Delete progress message
        if progress_msg:
            try:
                await progress_msg.delete()
            except Exception:
                pass

        # Record stats
        duration = asyncio.get_event_loop().time() - start_time
        ls = runner.get_last_stats()
        record_invocation(
            "chat", runner.get_model(), duration,
            input_tokens=ls.input_tokens if ls else 0,
            output_tokens=ls.output_tokens if ls else 0,
            cache_read=ls.cache_read if ls else 0,
            cache_creation=ls.cache_creation if ls else 0,
            cost_usd=ls.cost_usd if ls else 0,
            context_window=ls.context_window if ls else 0,
        )

        # Final output
        # reply = plan text (or "Думаю..." if no plan)
        # progress_msg = deleted
        # result = new message or replaces reply if no plan

        if had_actions and result_texts:
            result = "\n\n".join(result_texts).strip()
            formatted = md_to_tg(result)
            chunks = split_message(formatted)
            if plan_texts:
                # Plan shown in reply → keep it, result as new message
                for ch in chunks:
                    await safe_send(context.bot, chat_id, ch)
            else:
                # No plan → put result in reply (replace "Думаю...")
                await safe_edit(reply, chunks[0])
                for ch in chunks[1:]:
                    await safe_send(context.bot, chat_id, ch)
        elif had_actions and not result_texts:
            if not plan_texts:
                # Only actions, no text at all
                await safe_edit(reply, "✅ Выполнено", use_html=False)
            # else: plan in reply, nothing more to add
        elif not had_actions and plan_texts:
            # Simple response → finalize plan in reply
            final = "\n\n".join(plan_texts).strip()
            formatted = md_to_tg(final)
            chunks = split_message(formatted)
            await safe_edit(reply, chunks[0])
            for ch in chunks[1:]:
                await safe_send(context.bot, chat_id, ch)
        else:
            await safe_edit(reply, "[Пустой ответ]", use_html=False)

    except asyncio.CancelledError:
        stop_anim.set()
        try:
            text = accumulated.strip()
            await reply.edit_text((text + "\n\n[Отменено]") if text else "[Отменено]")
        except Exception:
            pass
    except Exception as e:
        stop_anim.set()
        logger.exception("Error processing message")
        try:
            await reply.edit_text(f"Ошибка: {e}")
        except Exception:
            pass


# ─── File/photo/voice handlers ───

@owner_only
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc = msg.document
    caption = msg.caption or ""

    # Media group — delegate to collector
    if msg.media_group_id:
        await _collect_media_group(update, context, msg)
        return

    file = await context.bot.get_file(doc.file_id)
    path = f"/tmp/tg_files/{doc.file_name}"
    os.makedirs("/tmp/tg_files", exist_ok=True)
    await file.download_to_drive(path)

    parts = _build_prompt_parts(msg)
    parts.append(f"Загружен файл: {path} (имя: {doc.file_name}). {caption}")
    prompt = "\n".join(parts)

    task = asyncio.create_task(_process_message(update, context, prompt))
    _active_tasks[update.effective_chat.id] = task


@owner_only
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    photo = msg.photo[-1]
    caption = msg.caption or "Проанализируй изображение"

    # Media group — delegate to collector
    if msg.media_group_id:
        await _collect_media_group(update, context, msg)
        return

    file = await context.bot.get_file(photo.file_id)
    path = f"/tmp/tg_files/photo_{photo.file_id}.jpg"
    os.makedirs("/tmp/tg_files", exist_ok=True)
    await file.download_to_drive(path)

    parts = _build_prompt_parts(msg)
    parts.append(f"Загружено фото: {path}. {caption}")
    prompt = "\n".join(parts)

    task = asyncio.create_task(_process_message(update, context, prompt))
    _active_tasks[update.effective_chat.id] = task


@owner_only
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    voice = msg.voice
    file = await context.bot.get_file(voice.file_id)
    path = f"/tmp/tg_files/voice_{voice.file_id}.ogg"
    os.makedirs("/tmp/tg_files", exist_ok=True)
    await file.download_to_drive(path)

    parts = _build_prompt_parts(msg)
    whisper = "/root/.openclaw/scripts/whisper-transcribe.sh"
    if os.path.exists(whisper):
        parts.append(f"Транскрибируй голосовое: bash {whisper} {path}. Потом ответь на содержимое.")
    else:
        parts.append(f"Голосовое сообщение: {path}")
    prompt = "\n".join(parts)

    task = asyncio.create_task(_process_message(update, context, prompt))
    _active_tasks[update.effective_chat.id] = task


# ─── Init ───

async def post_init(app: Application):
    commands = [
        BotCommand("start", "Помощь"),
        BotCommand("new", "Новая сессия"),
        BotCommand("stop", "Отменить текущее"),
        BotCommand("status", "Статус и usage"),
        BotCommand("crons", "Список кронов"),
        BotCommand("model", "Управление моделями"),
    ]
    await app.bot.set_my_commands(commands)
    cfg = load_models_config()
    runner.set_model(cfg.get("default", DEFAULT_MODEL))
    logger.info(f"Bot @{(await app.bot.get_me()).username} started!")


def main():
    os.makedirs("/root/.claude-bridge", exist_ok=True)

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("crons", cmd_crons))
    app.add_handler(CommandHandler("model", cmd_model))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logger.info("Starting Claude Code Telegram Bridge...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
