"""Turn an incident record into paste-ready report text for VRChat reports."""

import time
from pathlib import Path


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(ts))


def build_report(inc: dict) -> str:
    lines = [
        "=== VRChat Incident Report ===",
        f"Time:      {_fmt_time(inc['created_at'])}",
        f"World:     {inc['world_name'] or '(unknown)'}",
    ]
    if inc.get("world_id"):
        lines.append(f"World ID:  {inc['world_id']}")
    if inc.get("instance_id"):
        lines.append(f"Instance:  {inc['instance_id']}")
    lines += [
        "",
        f"Trigger:   \"{inc['trigger']}\" (detected by local speech-to-text "
        "on voice chat audio)",
    ]

    if inc.get("transcript"):
        lines += ["", "Transcript around the moment (automatic, may contain "
                      "recognition errors):"]
        lines += [f"  {t}" for t in inc["transcript"]]

    if inc.get("clip_path"):
        lines += ["", f"Video evidence: {Path(inc['clip_path']).name}",
                  f"  (local file: {inc['clip_path']})"]

    if inc.get("screenshot_path"):
        lines += [f"Screenshot at trigger (shows who was in view): "
                  f"{Path(inc['screenshot_path']).name}",
                  f"  (local file: {inc['screenshot_path']})"]

    if inc.get("players"):
        lines += ["", f"Players in instance at the time "
                      f"({len(inc['players'])}):"]
        for p in sorted(inc["players"], key=lambda p: p["name"].lower()):
            uid = f"  [{p['user_id']}]" if p.get("user_id") else ""
            lines.append(f"  - {p['name']}{uid}")

    if inc.get("notes"):
        lines += ["", "Moderator notes:", inc["notes"]]

    return "\n".join(lines)
