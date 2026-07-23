"""Incident records: what fired, who was there, and which clip has the proof.

Incidents are stored as one JSON object per line in incidents.jsonl next to
this file — human-readable, greppable, and safe to back up or hand to another
moderator.
"""

import json
import time
import uuid
from pathlib import Path

from autoclip import HERE

INCIDENTS_PATH = HERE / "incidents.jsonl"
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
    def __init__(self, path: Path = INCIDENTS_PATH):
        self.path = path
        self.incidents: list[dict] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        self.incidents.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    def add(self, *, trigger: str, transcript: list[str], world_name: str,
            world_id: str, instance_id: str, players: list[dict]) -> dict:
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
        }
        self.incidents.append(inc)
        self._append(inc)
        return inc

    def get(self, inc_id: str) -> dict | None:
        return next((i for i in self.incidents if i["id"] == inc_id), None)

    def update(self, inc_id: str, **fields) -> dict | None:
        inc = self.get(inc_id)
        if inc:
            inc.update(fields)
            self._rewrite()
        return inc

    def delete(self, inc_id: str) -> None:
        self.incidents = [i for i in self.incidents if i["id"] != inc_id]
        self._rewrite()

    # ---------------- persistence ----------------
    def _append(self, inc: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(inc, ensure_ascii=False) + "\n")

    def _rewrite(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for inc in self.incidents:
                f.write(json.dumps(inc, ensure_ascii=False) + "\n")
        tmp.replace(self.path)
