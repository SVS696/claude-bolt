"""Track Claude Code usage with real stats from JSON output."""
import json
import os
import time
from datetime import datetime

USAGE_FILE = "/root/.claude-bridge/usage.json"


def _load() -> dict:
    try:
        with open(USAGE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"invocations": []}


def _save(data: dict):
    os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
    with open(USAGE_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def record_invocation(source: str, model: str, duration_sec: float = 0,
                      input_tokens: int = 0, output_tokens: int = 0,
                      cache_read: int = 0, cache_creation: int = 0,
                      cost_usd: float = 0, context_window: int = 0):
    data = _load()
    data["invocations"].append({
        "ts": datetime.now().isoformat(),
        "epoch": time.time(),
        "source": source,
        "model": model,
        "duration": round(duration_sec, 1),
        "in": input_tokens,
        "out": output_tokens,
        "cache_r": cache_read,
        "cache_w": cache_creation,
        "cost": round(cost_usd, 6),
        "ctx": context_window,
    })
    # Keep last 7 days
    cutoff = time.time() - 7 * 86400
    data["invocations"] = [i for i in data["invocations"] if i.get("epoch", 0) > cutoff]
    _save(data)


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def _model_short(model: str) -> str:
    for k in ["opus", "sonnet", "haiku"]:
        if k in model.lower():
            return k
    return model[:15]


def _bar(used: int, total: int, w: int = 10) -> str:
    if total <= 0:
        return "░" * w
    f = min(int(used / total * w), w)
    return "█" * f + "░" * (w - f)


def get_usage_summary() -> str:
    """Get usage summary for /status."""
    data = _load()
    invs = data.get("invocations", [])
    now = time.time()

    last_24h = [i for i in invs if now - i.get("epoch", 0) < 86400]
    last_7d = invs

    # Tokens and cost
    in_24h = sum(i.get("in", 0) for i in last_24h)
    out_24h = sum(i.get("out", 0) for i in last_24h)
    cache_r = sum(i.get("cache_r", 0) for i in last_24h)
    cache_w = sum(i.get("cache_w", 0) for i in last_24h)
    cost_24h = sum(i.get("cost", 0) for i in last_24h)
    cost_7d = sum(i.get("cost", 0) for i in last_7d)

    chat_24h = sum(1 for i in last_24h if i.get("source") == "chat")
    cron_24h = sum(1 for i in last_24h if i.get("source", "").startswith("cron"))

    # Per-model breakdown
    models = {}
    for i in last_24h:
        m = _model_short(i.get("model", "?"))
        if m not in models:
            models[m] = {"calls": 0, "in": 0, "out": 0, "cost": 0}
        models[m]["calls"] += 1
        models[m]["in"] += i.get("in", 0)
        models[m]["out"] += i.get("out", 0)
        models[m]["cost"] += i.get("cost", 0)

    # Context window from last invocation
    ctx = 0
    if invs:
        ctx = invs[-1].get("ctx", 0)

    # Cache hit rate
    total_in = in_24h + cache_r
    cache_pct = int(cache_r / total_in * 100) if total_in > 0 else 0

    # Avg duration
    durs = [i.get("duration", 0) for i in last_24h if i.get("duration", 0) > 0]
    avg_dur = sum(durs) / len(durs) if durs else 0

    lines = []

    # Token stats
    lines.append(f"📊 Tokens 24h: {fmt_tokens(in_24h)} in / {fmt_tokens(out_24h)} out")
    lines.append(f"📦 Cache: {cache_pct}% hit · {fmt_tokens(cache_r)} cached, {fmt_tokens(cache_w)} new")

    if ctx:
        lines.append(f"🧠 Context: {fmt_tokens(ctx)}")

    # Cost
    lines.append(f"💰 Cost: ${cost_24h:.4f} (24h) · ${cost_7d:.4f} (7d)")

    # Calls
    lines.append(f"📈 Calls 24h: {len(last_24h)} (chat: {chat_24h}, cron: {cron_24h}) · 7d: {len(last_7d)}")

    # Per-model
    if models:
        parts = []
        for m, d in sorted(models.items()):
            parts.append(f"{m}: {d['calls']}×{fmt_tokens(d['in']+d['out'])}")
        lines.append(f"🏷 {' · '.join(parts)}")

    if avg_dur > 0:
        lines.append(f"⏱ Avg: {avg_dur:.0f}s")

    return "\n".join(lines)
