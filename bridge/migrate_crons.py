#!/usr/bin/env python3
"""Migrate OpenClaw cron jobs to system crontab + Claude Code."""
import json
import os
import sys
import subprocess

CRON_WRAPPER = "/root/claude-telegram-bridge/cron_wrapper.sh"
JOBS_FILE = "/root/.openclaw/cron/jobs.json"
PROMPTS_DIR = "/root/claude-telegram-bridge/cron-prompts"
OUTPUT_CRONTAB = "/root/claude-telegram-bridge/generated-crontab"


def load_jobs():
    with open(JOBS_FILE) as f:
        data = json.load(f)
    return data["jobs"]


def tz_to_env(tz: str) -> str:
    """Return TZ env var prefix for crontab."""
    if tz and tz != "UTC":
        return f'TZ="{tz}" '
    return ""


def sanitize_name(name: str) -> str:
    """Create safe filename from job name."""
    safe = name.lower()
    for ch in " /()@":
        safe = safe.replace(ch, "-")
    for ch in "⚡💰⏰📊":
        safe = safe.replace(ch, "")
    safe = safe.strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe or "unnamed"


def main():
    os.makedirs(PROMPTS_DIR, exist_ok=True)
    jobs = load_jobs()

    crontab_lines = [
        "# Claude Code cron jobs - migrated from OpenClaw",
        f"# Generated: {__import__('datetime').datetime.now().isoformat()}",
        "# Wrapper: cron_wrapper.sh <name> <prompt-file> [timeout]",
        "",
        "SHELL=/bin/bash",
        'PATH=/root/.local/share/fnm/node-versions/v24.13.1/installation/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        "",
    ]

    enabled_count = 0
    disabled_count = 0

    for job in jobs:
        name = job.get("name", "unnamed")
        enabled = job.get("enabled", False)
        schedule = job.get("schedule", {})
        expr = schedule.get("expr")
        tz = schedule.get("tz", "UTC")
        payload = job.get("payload", {})
        message = payload.get("message", "")

        if not expr or expr == "?":
            # Interval-based jobs (like Yandex Calendar every 15min)
            interval = schedule.get("interval")
            if interval:
                minutes = interval // 60000 if interval > 60000 else 15
                expr = f"*/{minutes} * * * *"
            else:
                print(f"  SKIP (no schedule): {name}")
                disabled_count += 1
                continue

        safe_name = sanitize_name(name)

        # Save prompt to file
        prompt_file = os.path.join(PROMPTS_DIR, f"{safe_name}.txt")
        with open(prompt_file, "w") as f:
            f.write(message)

        # Determine timeout
        timeout = 600
        if "digest" in name.lower() or "report" in name.lower() or "сводк" in name.lower():
            timeout = 900  # 15 min for heavy tasks

        # Build crontab line
        tz_env = tz_to_env(tz)
        prefix = "# " if not enabled else ""
        comment = f"# {name}"
        if not enabled:
            comment += " [DISABLED]"

        crontab_lines.append(comment)
        crontab_lines.append(
            f'{prefix}{tz_env}{expr} {CRON_WRAPPER} "{safe_name}" "$(cat {prompt_file})" {timeout}'
        )
        crontab_lines.append("")

        if enabled:
            enabled_count += 1
        else:
            disabled_count += 1

        print(f"  {'OK' if enabled else 'DISABLED'}: {name} -> {safe_name} ({expr} {tz})")

    # Write generated crontab
    with open(OUTPUT_CRONTAB, "w") as f:
        f.write("\n".join(crontab_lines))

    print(f"\n{'='*50}")
    print(f"Enabled: {enabled_count}, Disabled: {disabled_count}")
    print(f"Crontab written to: {OUTPUT_CRONTAB}")
    print(f"Prompts saved to: {PROMPTS_DIR}/")
    print(f"\nTo install: crontab {OUTPUT_CRONTAB}")
    print(f"To review:  cat {OUTPUT_CRONTAB}")


if __name__ == "__main__":
    main()
