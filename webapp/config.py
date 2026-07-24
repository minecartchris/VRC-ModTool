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
    # the sync API entirely.
    "sync_token": "",
    "session_hours": 12,
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
