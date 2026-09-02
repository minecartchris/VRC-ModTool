"""Discord webhook for logged moderation actions.

Deliberately byte-compatible with the Teen Chillout web tool's
`src/app/api/logs/route.js`: same embed title, same colours, same field order.
Both tools post into the same channel, so a reader should not be able to tell
which one filed a given log.

Posting happens server-side for the same reason it does there — the webhook
URL stays out of the browser, and names and links are stripped of markdown so
a display name like `](evil)` cannot forge a link in the embed.
"""

import re
import threading

import requests

#: Matches route.js: Ban red, Warn yellow, everything else kick orange.
COLORS = {"Ban": 16711680, "Warn": 16776960}
DEFAULT_COLOR = 16734296

_MD = re.compile(r"[\[\]()]")


def _safe(text: str) -> str:
    """Strip the characters that make a markdown link, as route.js does."""
    return _MD.sub("", str(text or ""))


def profile_url(user_id: str) -> str:
    return f"https://vrchat.com/home/user/{user_id}" if user_id else "#"


def build_embed(*, action: str, moderator: str, reason: str, timestamp: str,
                targets: list[dict]) -> dict:
    if targets:
        users = "\n".join(
            f"• [{_safe(t.get('name') or 'Unknown')}]"
            f"({_safe(t.get('link') or profile_url(t.get('user_id', '')))})"
            for t in targets)
    else:
        users = "Unknown User"
    return {
        "title": f"🚨 New {action or 'Kick'} Log Submitted",
        "color": COLORS.get(action, DEFAULT_COLOR),
        "fields": [
            {"name": "Moderator", "value": _safe(moderator) or "Unknown",
             "inline": True},
            {"name": "Target Users", "value": users, "inline": False},
            {"name": "Reason", "value": _safe(reason) or "—", "inline": False},
            {"name": "Date/Time", "value": timestamp, "inline": False},
        ],
        "timestamp": timestamp,
    }


def post(cfg: dict, *, action: str, moderator: str, reason: str,
         timestamp: str, targets: list[dict], record=None,
         incident_id: str = "") -> None:
    """Fire the webhooks in the background.

    Never raises and never blocks the request: a moderator's reason is already
    saved by the time this runs, and a dead webhook must not make it look like
    the log failed.
    """
    # Actions the channel does not want. The log, the incident and the age
    # check are still recorded — this only decides what gets announced.
    skip = {str(a).strip().lower()
            for a in (cfg.get("discord_skip_actions") or [])}
    if (action or "").strip().lower() in skip:
        return

    url = (cfg.get("discord_webhook_url") or "").strip()
    overaged_url = (cfg.get("overaged_webhook_url") or "").strip()
    if not url and not overaged_url:
        return

    embed = build_embed(action=action, moderator=moderator, reason=reason,
                        timestamp=timestamp, targets=targets)

    who = ", ".join((t.get("name") or "?") for t in targets)[:120]

    def note(status: int, error: str = "") -> None:
        # Written whatever happens, so "the channel shows two, we sent one"
        # is a question with an answer.
        if record:
            try:
                record(incident_id, action, who, reason, moderator,
                       status, error)
            except Exception:
                pass

    def send() -> None:
        if url:
            try:
                r = requests.post(url, json={"embeds": [embed]}, timeout=15)
                note(r.status_code)
            except requests.RequestException as e:
                note(0, str(e)[:180])
        # Second channel for age removals, matching route.js.
        if overaged_url and "overage" in (reason or "").lower():
            links = "\n".join(
                t.get("link") or profile_url(t.get("user_id", ""))
                for t in targets) or "No link provided"
            try:
                requests.post(overaged_url, json={
                    "content": f"**Overaged User Kicked** (by {_safe(moderator)}):"
                               f"\n{links}"}, timeout=15)
            except requests.RequestException:
                pass

    threading.Thread(target=send, daemon=True).start()
