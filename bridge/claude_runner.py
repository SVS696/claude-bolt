"""Claude Code CLI runner with usage tracking via JSON output."""
import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator

from config import CLAUDE_BIN, CLAUDE_WORKSPACE, FNM_BIN, SESSION_DIR

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"


@dataclass
class RunStats:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    cost_usd: float = 0
    duration_ms: int = 0
    model: str = ""
    context_window: int = 0


@dataclass
class ClaudeSession:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    chat_id: int = 0
    claude_session_id: str = ""  # Real session ID from Claude Code JSON output


class ClaudeRunner:
    def __init__(self):
        os.makedirs(SESSION_DIR, exist_ok=True)
        self._sessions: dict[int, ClaudeSession] = {}
        self._model: str = DEFAULT_MODEL
        self._last_stats: RunStats | None = None

    def set_model(self, model: str):
        self._model = model

    def get_model(self) -> str:
        return self._model

    def get_last_stats(self) -> RunStats | None:
        return self._last_stats

    def get_session(self, chat_id: int) -> ClaudeSession:
        if chat_id not in self._sessions:
            self._sessions[chat_id] = ClaudeSession(chat_id=chat_id)
        return self._sessions[chat_id]

    def new_session(self, chat_id: int) -> ClaudeSession:
        self._sessions[chat_id] = ClaudeSession(chat_id=chat_id)
        return self._sessions[chat_id]

    def _build_env(self) -> dict:
        env = os.environ.copy()
        env["PATH"] = f"{FNM_BIN}:{env.get('PATH', '')}"
        env["NODE_NO_WARNINGS"] = "1"
        return env

    def _parse_json_result(self, output: str) -> tuple[str, RunStats]:
        """Parse JSON output format to extract text and stats."""
        stats = RunStats(model=self._model)
        text = ""

        try:
            data = json.loads(output)
            text = data.get("result", "")

            usage = data.get("usage", {})
            stats.input_tokens = usage.get("input_tokens", 0)
            stats.output_tokens = usage.get("output_tokens", 0)
            stats.cache_read = usage.get("cache_read_input_tokens", 0)
            stats.cache_creation = usage.get("cache_creation_input_tokens", 0)
            stats.cost_usd = data.get("total_cost_usd", 0)
            stats.duration_ms = data.get("duration_ms", 0)

            # Get model info from modelUsage
            model_usage = data.get("modelUsage", {})
            for model_name, model_data in model_usage.items():
                stats.model = model_name
                stats.context_window = model_data.get("contextWindow", 0)
                # Use more accurate per-model stats
                stats.input_tokens = model_data.get("inputTokens", stats.input_tokens)
                stats.output_tokens = model_data.get("outputTokens", stats.output_tokens)
                stats.cache_read = model_data.get("cacheReadInputTokens", stats.cache_read)
                stats.cache_creation = model_data.get("cacheCreationInputTokens", stats.cache_creation)
                stats.cost_usd = model_data.get("costUSD", stats.cost_usd)
                break  # Take first model

        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to parse JSON output: {e}")
            text = output  # Fallback: treat as plain text

        return text, stats

    async def run(self, message: str, chat_id: int, model: str | None = None) -> AsyncIterator[str]:
        """Run Claude Code with stream-json output, showing tool actions in real-time."""
        session = self.get_session(chat_id)
        use_model = model or self._model

        cmd = [
            CLAUDE_BIN,
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            "--model", use_model,
        ]
        if session.claude_session_id:
            cmd.extend(["--resume", session.claude_session_id])
        cmd.append(message)
        logger.info(f"Running claude model={use_model} resume={session.claude_session_id or 'new'}...")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._build_env(),
            cwd=CLAUDE_WORKSPACE,
        )

        try:
            result_text = ""
            stats = RunStats(model=use_model)

            async def read_stream():
                nonlocal result_text, stats
                async for raw_line in process.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type")

                    if event_type == "assistant":
                        content = event.get("message", {}).get("content", [])
                        for block in content:
                            if block.get("type") == "text" and block.get("text", "").strip():
                                yield f"\x01TEXT:{block['text']}"
                            elif block.get("type") == "tool_use":
                                action = self._format_tool_action(block.get("name", ""), block.get("input", {}))
                                if action:
                                    yield f"\x01ACTION:{action}"

                    elif event_type == "result":
                        result_text = event.get("result", "")
                        sid = event.get("session_id", "")
                        if sid:
                            session.claude_session_id = sid
                            logger.info(f"Session: {sid[:8]}...")

                        # Parse stats
                        usage = event.get("usage", {})
                        stats.input_tokens = usage.get("input_tokens", 0)
                        stats.output_tokens = usage.get("output_tokens", 0)
                        stats.cache_read = usage.get("cache_read_input_tokens", 0)
                        stats.cache_creation = usage.get("cache_creation_input_tokens", 0)
                        stats.cost_usd = event.get("total_cost_usd", 0)
                        stats.duration_ms = event.get("duration_ms", 0)

                        model_usage = event.get("modelUsage", {})
                        for model_name, model_data in model_usage.items():
                            stats.model = model_name
                            stats.context_window = model_data.get("contextWindow", 0)
                            stats.input_tokens = model_data.get("inputTokens", stats.input_tokens)
                            stats.output_tokens = model_data.get("outputTokens", stats.output_tokens)
                            stats.cache_read = model_data.get("cacheReadInputTokens", stats.cache_read)
                            stats.cache_creation = model_data.get("cacheCreationInputTokens", stats.cache_creation)
                            stats.cost_usd = model_data.get("costUSD", stats.cost_usd)
                            break

            async for chunk in read_stream():
                yield chunk

            # Wait for process to finish
            await asyncio.wait_for(process.wait(), timeout=600)

            if process.returncode != 0:
                stderr_data = await process.stderr.read()
                err = stderr_data.decode("utf-8", errors="replace")[:500] if stderr_data else ""
                logger.error(f"Claude error (rc={process.returncode}): {err}")
                yield f"[Error: {err}]"
                return

            self._last_stats = stats

            # Signal end of stream (TEXT blocks already yielded the content)
            yield "\x01DONE:"

        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            yield "\x01RESULT:[Timeout: 10 min exceeded]"

    @staticmethod
    def _format_tool_action(tool_name: str, tool_input: dict) -> str:
        """Format a tool use event as a readable action string."""
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if len(cmd) > 80:
                cmd = cmd[:77] + "..."
            return f"$ {cmd}"
        elif tool_name == "Edit":
            path = tool_input.get("file_path", "")
            return f"✏️ {os.path.basename(path)}"
        elif tool_name == "Write":
            path = tool_input.get("file_path", "")
            return f"📝 {os.path.basename(path)}"
        elif tool_name == "Read":
            path = tool_input.get("file_path", "")
            return f"👁 {os.path.basename(path)}"
        elif tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            return f"🔍 {pattern}"
        else:
            return f"{tool_name}"

    def _get_last_session_id(self) -> str:
        """Read last session ID from .claude.json."""
        try:
            with open("/root/.claude.json") as f:
                data = json.load(f)
            for proj_data in data.get("projects", {}).values():
                sid = proj_data.get("lastSessionId", "")
                if sid:
                    return sid
        except Exception as e:
            logger.error(f"Failed to read session ID: {e}")
        return ""

    async def _get_last_run_stats(self, model: str) -> RunStats:
        """Read last run stats from .claude.json."""
        stats = RunStats(model=model)
        try:
            with open("/root/.claude.json") as f:
                data = json.load(f)
            # Find the project data
            for proj_path, proj_data in data.get("projects", {}).items():
                stats.input_tokens = proj_data.get("lastTotalInputTokens", 0)
                stats.output_tokens = proj_data.get("lastTotalOutputTokens", 0)
                stats.cache_read = proj_data.get("lastTotalCacheReadInputTokens", 0)
                stats.cache_creation = proj_data.get("lastTotalCacheCreationInputTokens", 0)
                stats.cost_usd = proj_data.get("lastCost", 0)
                stats.duration_ms = proj_data.get("lastAPIDuration", 0)
                # Get model usage for context window
                for mname, mdata in proj_data.get("lastModelUsage", {}).items():
                    stats.model = mname
                    stats.context_window = mdata.get("contextWindow", 0) if isinstance(mdata, dict) else 0
                break
        except Exception as e:
            logger.error(f"Failed to read .claude.json stats: {e}")
        return stats

    async def get_quick_stats(self) -> RunStats:
        """Run a minimal query with json format to get current usage stats."""
        cmd = [
            CLAUDE_BIN,
            "--print",
            "--output-format", "json",
            "--model", self._model,
            "ok",
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._build_env(),
            cwd=CLAUDE_WORKSPACE,
        )

        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
            output = stdout.decode("utf-8", errors="replace")
            _, stats = self._parse_json_result(output)
            return stats
        except Exception as e:
            logger.error(f"Quick stats failed: {e}")
            return RunStats(model=self._model)
