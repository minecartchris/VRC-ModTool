"""Server settings, loaded from web_config.json next to the repo root.

Kept separate from the desktop's config.json: that file holds one moderator's
personal preferences, this one holds deployment settings (who counts as staff,
what token desktop clients sync with). Both are gitignored.
"""

import json
import os
from pathlib import Path

from paths import HERE

CONFIG_PATH = Path(os.environ.get("MODTOOL_WEB_CONFIG")
                   or (HERE / "web_config.json"))

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8787,
    # Who may sign in: a VRChat group ID (grp_...), its short code, or a
    # substring of its name. Empty means nobody — login fails closed.
    "staff_group": "",
    # VRChat's WAF rejects API calls whose User-Agent lacks real contact info.
    "vrc_contact": "",
    # Shared secret desktop clients present on /api/sync/*. Empty disables
    # the sync API entirely. Grants full read/write to every record.
    "sync_token": "",
    # Weaker secret accepted only on /api/sync/roster. This is what gets baked
    # into the distributed roster agent: anyone can pull it out of the binary,
    # so it must not be able to read incidents or age checks — it can only say
    # who is in an instance.
    "roster_token": "",
    "session_hours": 12,
    # Restart the server when a .py file changes. On by default because this
    # is normally run next to the desktop app while you are editing it; signed
    # -in moderators survive the restart, so it costs nothing. Turn it off when
    # hosting, where the file watcher is pure overhead.
    "auto_reload": True,
    # Watch this group's VRChat audit log for instance kicks and warns, and
    # ask the moderator who issued each one for a reason. Needs a signed-in
    # moderator holding `group-audit-view` on that group. Empty disables it.
    "audit_group": "",
    "audit_poll_seconds": 60,
    # How long a kick or warn keeps asking for a reason before it gives up.
    # A day later nobody remembers which of four people it was, and a page of
    # stale prompts is a page people stop reading. 0 turns expiry off.
    "pending_expire_hours": 12,
    # Where a completed log is announced. Same embed format as the Teen
    # Chillout web tool, so both can post to the same channel.
    "discord_webhook_url": "",
    # Second channel for age removals, mirroring that tool's behaviour.
    "overaged_webhook_url": "",
    # The group people are invited to and banned from. Empty falls back to
    # `roster_group`, which is normally the same group again.
    "action_group": "",
    # Ban an overage kick's target from the group, once a moderator holding
    # `group-bans-manage` is signed in. Off by default: it acts on a real
    # person from an automated rule, so a deployment has to ask for it.
    "auto_ban_overage": False,
    # Queue the bans but do not carry them out. Everything an overage kick
    # would ban is recorded and visible on the Admin page, and nothing is sent
    # to VRChat — the way to watch what the rule *would* do to real people
    # before letting it. Turning this off later releases the whole backlog, so
    # look at it first.
    "hold_bans": False,
    # Invite anyone a moderator verifies as in-range, if they are not already
    # in the group. Needs `group-invites-manage`.
    "auto_invite_verified": False,
    # The same brake for invites. Chiefly a stop button for a large backfill:
    # set it and the queue stops sending, keeping every row for later.
    "hold_invites": False,
    # Actions that are logged but not announced, e.g. ["Warn"] where warns are
    # routine and would drown the channel. Only the Discord post is skipped:
    # the incident, the age check and the Kick Log page are unaffected.
    "discord_skip_actions": [],
    # One-click chips on the Kick Log and the reason prompt. Same list the
    # Teen Chillout web tool offers (KickLogForm.jsx COMMON_REASONS), so
    # reasons stay comparable between the two tools. Moderators can add their
    # own on top, stored per account.
    "common_reasons": [
        "Age Baiting", "Avatar", "Ban Evasion", "Blocked or muted mod",
        "Cat Calling", "Disrespect Staff", "Harassment",
        "Inappropriate Comments", "Loud audio", "Microphone spam", "Overaged",
        "Racist Remarks", "Refused Age Check", "Refusing Requests From Mod",
        "Repeated use of slur", "Said slur", "Sexual Comments", "Spamming",
        "Underaged",
    ],
    # Tail this machine's VRChat log for the instance roster, so Screening
    # works without the desktop app running. Ignored automatically when
    # there is no VRChat log directory (e.g. a Linux host), where the roster
    # then comes only from what desktop clients push over sync.
    "read_local_log": True,
    # Let the web UI write VRChat user notes (the "In Range" tag). Needs the
    # signed-in moderator's live API session.
    "note_filter": "age ok",
    # Serve clip/screenshot files recorded by desktop clients. Only makes
    # sense when the server runs on the same machine that captured them.
    "serve_media": True,
    # Extra directories the server may read media from. incident_shots/ is
    # always allowed; add your Medal clips folder here to play clips in the
    # browser. Paths outside these roots are refused, because incident records
    # can arrive over the sync API and must not be able to name any file.
    "media_roots": [],
    # Set True only behind HTTPS; marks the session cookie Secure.
    "https_only": False,
    # Only accept rosters from instances this group owns, matched against the
    # `~group(grp_…)` VRChat writes into the instance id. Empty accepts any.
    # Normally the same group as `audit_group`: a moderator sitting in a
    # private world or a friend's instance is not on duty, and that roster has
    # no business on the Screening page.
    "roster_group": "",
    # VRChat ids who are always admins, whatever the admins table says — the
    # backstop against the last admin removing themselves and locking the
    # tool's own administration out. Everyone else is appointed in the UI.
    "root_admins": [],
    # The packaged roster agent, offered for download on a moderator's settings
    # page. Empty falls back to dist/VRChatRosterAgent.exe next to the code,
    # which is where build_agent.py leaves it; a hosted deployment points this
    # at wherever the build was uploaded. Missing just hides the button.
    "agent_exe": "",
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        except (OSError, json.JSONDecodeError):
            pass
    # Environment wins, so a container can be configured without a file.
    for key in ("staff_group", "vrc_contact", "sync_token", "host"):
        env = os.environ.get(f"MODTOOL_{key.upper()}")
        if env:
            cfg[key] = env
    if os.environ.get("MODTOOL_PORT"):
        cfg["port"] = int(os.environ["MODTOOL_PORT"])
    return cfg


def save(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
