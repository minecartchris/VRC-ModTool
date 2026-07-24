"""Single SQLite store for the mod suite: incidents + the screening cache.

Everything lives in one file, modtool.db, next to this module. Connections are
opened with check_same_thread=False and guarded by a lock, because both the Tk
GUI thread and the screening background worker read and write.

On first run it imports any pre-existing incidents.jsonl / screening_db.json
(the old JSON stores) so no history is lost, then leaves those files untouched.
"""

import json
import sqlite3
import threading
from pathlib import Path

from autoclip import HERE

DB_PATH = HERE / "modtool.db"
OLD_INCIDENTS = HERE / "incidents.jsonl"
OLD_SCREENING = HERE / "screening_db.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id              TEXT PRIMARY KEY,
    created_at      REAL,
    trigger         TEXT,
    transcript      TEXT,   -- JSON array
    world_name      TEXT,
    world_id        TEXT,
    instance_id     TEXT,
    players         TEXT,   -- JSON array
    clip_path       TEXT,
    screenshot_path TEXT,
    notes           TEXT,
    status          TEXT
);
CREATE TABLE IF NOT EXISTS screening_users (
    user_id    TEXT PRIMARY KEY,
    name       TEXT,
    note       TEXT,
    groups     TEXT,        -- JSON array
    checked_at REAL
);
"""


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
        self._migrate_json()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    def _count(self, table: str) -> int:
        with self._lock:
            return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # ---------------- incidents ----------------
    def all_incidents(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM incidents ORDER BY created_at").fetchall()
        return [self._row_to_incident(r) for r in rows]

    def upsert_incident(self, inc: dict) -> None:
        self._exec(
            """INSERT INTO incidents
               (id, created_at, trigger, transcript, world_name, world_id,
                instance_id, players, clip_path, screenshot_path, notes, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 created_at=excluded.created_at, trigger=excluded.trigger,
                 transcript=excluded.transcript, world_name=excluded.world_name,
                 world_id=excluded.world_id, instance_id=excluded.instance_id,
                 players=excluded.players, clip_path=excluded.clip_path,
                 screenshot_path=excluded.screenshot_path, notes=excluded.notes,
                 status=excluded.status""",
            (inc["id"], inc.get("created_at"), inc.get("trigger", ""),
             json.dumps(inc.get("transcript", []), ensure_ascii=False),
             inc.get("world_name", ""), inc.get("world_id", ""),
             inc.get("instance_id", ""),
             json.dumps(inc.get("players", []), ensure_ascii=False),
             inc.get("clip_path", ""), inc.get("screenshot_path", ""),
             inc.get("notes", ""), inc.get("status", "new")))

    def delete_incident(self, inc_id: str) -> None:
        self._exec("DELETE FROM incidents WHERE id=?", (inc_id,))

    @staticmethod
    def _row_to_incident(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"], "created_at": r["created_at"],
            "trigger": r["trigger"] or "",
            "transcript": json.loads(r["transcript"] or "[]"),
            "world_name": r["world_name"] or "", "world_id": r["world_id"] or "",
            "instance_id": r["instance_id"] or "",
            "players": json.loads(r["players"] or "[]"),
            "clip_path": r["clip_path"] or "",
            "screenshot_path": r["screenshot_path"] or "",
            "notes": r["notes"] or "", "status": r["status"] or "new"}

    # ---------------- screening cache ----------------
    def all_users(self) -> dict:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM screening_users").fetchall()
        return {r["user_id"]: {
            "name": r["name"] or "", "note": r["note"] or "",
            "groups": json.loads(r["groups"] or "[]"),
            "checked_at": r["checked_at"]} for r in rows}

    def upsert_user(self, uid: str, rec: dict) -> None:
        self._exec(
            """INSERT INTO screening_users (user_id, name, note, groups, checked_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 name=excluded.name, note=excluded.note,
                 groups=excluded.groups, checked_at=excluded.checked_at""",
            (uid, rec.get("name", ""), rec.get("note", ""),
             json.dumps(rec.get("groups", []), ensure_ascii=False),
             rec.get("checked_at")))

    # ---------------- one-time migration ----------------
    def _migrate_json(self) -> None:
        if self._count("incidents") == 0 and OLD_INCIDENTS.exists():
            for line in OLD_INCIDENTS.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    inc = json.loads(line)
                    if inc.get("id"):
                        self.upsert_incident(inc)
                except (json.JSONDecodeError, KeyError):
                    pass
        if self._count("screening_users") == 0 and OLD_SCREENING.exists():
            try:
                data = json.loads(OLD_SCREENING.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            for uid, rec in data.items():
                if isinstance(rec, dict):
                    self.upsert_user(uid, rec)
