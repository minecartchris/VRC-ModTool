"""Incident records: what fired, who was there, and which clip has the proof.

Incidents are stored in the shared SQLite database (see db.py). This module
keeps the same IncidentStore API the GUI already uses; it just persists to the
`incidents` table instead of a JSONL file.
"""

import time
import uuid
from pathlib import Path

import db

DEFAULT_MEDAL_DIR = Path.home() / "Videos" / "Medal" / "Clips"


def find_new_clip(clips_dir: str | Path, after_ts: float) -> str | None:
    """Earliest video file created at/after after_ts (Medal saves within
    seconds of the hotkey press). Searches game subfolders too."""
    clips_dir = Path(clips_dir)
    if not clips_dir.exists():
        return None
    best: tuple[float, Path] | None = None
    for ext in ("*.mp4", "*.mkv", "*.webm"):
        for p in clips_dir.rglob(ext):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m >= after_ts - 2 and (best is None or m < best[0]):
                best = (m, p)
    return str(best[1]) if best else None


class IncidentStore:
    def __init__(self, database: "db.Database | None" = None):
        self.db = database or db.Database()
        self.incidents: list[dict] = self.db.all_incidents()

    def add(self, *, trigger: str, transcript: list[str], world_name: str,
            world_id: str, instance_id: str, players: list[dict],
            origin: str = "listener", reported_by: str = "") -> dict:
        inc = {
            "id": uuid.uuid4().hex[:12],
            "created_at": time.time(),
            "trigger": trigger,
            "transcript": list(transcript),
            "world_name": world_name,
            "world_id": world_id,
            "instance_id": instance_id,
            "players": players,
            "clip_path": "",
            "screenshot_path": "",
            "notes": "",
            "status": "new",           # new | reported | dismissed
            "origin": origin,          # listener | desktop | web | auto
            "reported_by": reported_by,
        }
        self.incidents.append(inc)
        self.db.upsert_incident(inc)
        return inc

    def reload(self) -> None:
        """Re-read from the database.

        The in-memory list is not the only writer any more: age checks and the
        sync client write incidents straight to the DB, so the GUI refreshes
        from it before redrawing rather than trusting its own cache.
        """
        self.incidents = self.db.all_incidents()

    def get(self, inc_id: str) -> dict | None:
        return next((i for i in self.incidents if i["id"] == inc_id), None)

    def update(self, inc_id: str, **fields) -> dict | None:
        inc = self.get(inc_id)
        if inc:
            inc.update(fields)
            self.db.upsert_incident(inc)
        return inc

    def delete(self, inc_id: str) -> None:
        self.incidents = [i for i in self.incidents if i["id"] != inc_id]
        self.db.delete_incident(inc_id)
