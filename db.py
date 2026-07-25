"""Single SQLite store for the mod suite: incidents, age checks, screening.

Everything lives in one file, modtool.db, next to this module. Connections are
opened with check_same_thread=False and guarded by a lock, because the Tk GUI
thread, the screening background worker, and (on the web branch) the server's
request handlers all read and write.

The same file backs both the desktop app and the web server, so writes carry
two extra fields the sync layer depends on:

    updated_at  bumped only when a record's content actually changes
    deleted     soft-delete flag, so a delete on one side propagates

Bumping `updated_at` only on a real change is what stops push/pull ping-pong:
a record pulled from the server and written back locally is byte-identical, so
it never re-enters the outbound queue. See sync.py.

On first run it imports any pre-existing incidents.jsonl / screening_db.json
(the old JSON stores) so no history is lost, then leaves those files untouched.
"""

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from paths import DB_PATH, HERE

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

-- Age checks: one row per verdict a moderator recorded on a player. The
-- desktop Screening tab and the web UI both write here; `incident_id` links
-- the row to the incident an over/under verdict files alongside it.
CREATE TABLE IF NOT EXISTS age_checks (
    id            TEXT PRIMARY KEY,
    user_id       TEXT,
    name          TEXT,
    verdict       TEXT,     -- over | under | in_range
    reported_age  INTEGER,
    world_name    TEXT,
    world_id      TEXT,
    instance_id   TEXT,
    incident_id   TEXT,
    checked_by    TEXT,     -- moderator display name
    checked_by_id TEXT,     -- moderator usr_ id
    note          TEXT,
    source        TEXT,     -- desktop | web | auto
    created_at    REAL,
    updated_at    REAL,
    deleted       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_age_checks_user ON age_checks(user_id);
CREATE INDEX IF NOT EXISTS ix_age_checks_updated ON age_checks(updated_at);

-- Web sessions. Created only after a VRChat login proves the account is in
-- the configured staff group.
--
-- `token` holds the SHA-256 of the session token, never the token itself, and
-- `vrc_cookie` holds that moderator's VRChat auth cookie encrypted with a key
-- derived from the same raw token. The raw token exists only in the browser
-- cookie, so this file on its own decrypts nothing: a stolen modtool.db yields
-- neither a usable session nor anyone's VRChat login. See webapp/auth.py.
CREATE TABLE IF NOT EXISTS web_sessions (
    token      TEXT PRIMARY KEY,   -- sha256(session token)
    user_id    TEXT,
    name       TEXT,
    groups     TEXT,               -- JSON array of matched staff groups
    created_at REAL,
    expires_at REAL,
    vrc_cookie TEXT                -- Fernet(key=KDF(session token)) blob
);

-- Last instance snapshot each reporter sent, so the web Screening page can
-- show who is in the world right now. A reporter is either this server
-- reading the local VRChat log, or a roster agent / desktop client elsewhere.
--
-- Two timestamps, and the difference matters: `updated_at` moves only when the
-- roster's contents change and feeds state_version(), so open browsers reload
-- on a real join or leave. `seen_at` moves on every heartbeat and is how we
-- know the reporter is still alive without reloading anyone's page.
CREATE TABLE IF NOT EXISTS rosters (
    client_id   TEXT PRIMARY KEY,
    client_name TEXT,
    world_name  TEXT,
    world_id    TEXT,
    instance_id TEXT,
    players     TEXT,       -- JSON array
    updated_at  REAL,       -- last content change
    seen_at     REAL        -- last heartbeat
);

-- The moderator allowlist, imported from the Teen Chillout web tool's
-- `allowed_users` collection. Access to this tool is still decided by VRChat
-- staff-group membership; this only records who holds which role, so the UI
-- can tell an HR from a Mod and attribute imported history correctly.
CREATE TABLE IF NOT EXISTS staff (
    user_id  TEXT PRIMARY KEY,
    name     TEXT,
    role     TEXT,        -- Mod | HR
    added_at REAL
);

-- Kicks and warns seen in the VRChat group audit log that still need a reason
-- from the moderator who issued them. The primary key is VRChat's own audit id
-- (gaud_...), so re-polling the same window can't queue an action twice.
CREATE TABLE IF NOT EXISTS pending_actions (
    id          TEXT PRIMARY KEY,   -- gaud_... from the audit log
    group_id    TEXT,
    action      TEXT,               -- Kick | Warn
    actor_id    TEXT,               -- the moderator who did it
    actor_name  TEXT,
    target_id   TEXT,
    target_name TEXT,
    location    TEXT,               -- world:instance it happened in
    created_at  REAL,               -- when VRChat recorded it
    noticed_at  REAL,
    reason      TEXT,
    resolved_at REAL,
    incident_id TEXT,
    dismissed   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_pending_actor ON pending_actions(actor_id);

-- Sync cursors, one row per peer ("server" on a desktop client).
CREATE TABLE IF NOT EXISTS sync_state (
    peer         TEXT PRIMARY KEY,
    last_push_at REAL DEFAULT 0,
    last_pull_at REAL DEFAULT 0
);
"""

# Columns added after the first release; applied to existing databases in
# _migrate_columns so an old modtool.db keeps working untouched.
_ADDED_COLUMNS = {
    "incidents": {
        "updated_at": "REAL",
        "deleted": "INTEGER DEFAULT 0",
        "reported_by": "TEXT",
        "origin": "TEXT",
    },
    "web_sessions": {
        "vrc_cookie": "TEXT",
    },
    "rosters": {
        "seen_at": "REAL",
    },
}

_INCIDENT_FIELDS = (
    "created_at", "trigger", "transcript", "world_name", "world_id",
    "instance_id", "players", "clip_path", "screenshot_path", "notes",
    "status", "reported_by", "origin", "deleted",
)
_AGE_CHECK_FIELDS = (
    "user_id", "name", "verdict", "reported_age", "world_name", "world_id",
    "instance_id", "incident_id", "checked_by", "checked_by_id", "note",
    "source", "created_at", "deleted",
)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            # Two processes (GUI + server) share this file; wait rather than
            # raising "database is locked" the moment they overlap.
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
        self._migrate_columns()
        self._migrate_json()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def _count(self, table: str) -> int:
        with self._lock:
            return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # ---------------- incidents ----------------
    def all_incidents(self, include_deleted: bool = False) -> list[dict]:
        sql = "SELECT * FROM incidents"
        if not include_deleted:
            sql += " WHERE COALESCE(deleted, 0) = 0"
        sql += " ORDER BY created_at"
        return [self._row_to_incident(r) for r in self._query(sql)]

    def get_incident(self, inc_id: str) -> dict | None:
        r = self._one("SELECT * FROM incidents WHERE id=?", (inc_id,))
        return self._row_to_incident(r) if r else None

    def upsert_incident(self, inc: dict) -> bool:
        """Write an incident. Returns True if anything actually changed.

        `updated_at` is stamped only on a real change, which keeps a record
        that round-trips through sync from looking perpetually dirty.
        """
        row = self._incident_row(inc)
        existing = self._one("SELECT * FROM incidents WHERE id=?", (inc["id"],))
        if existing and not inc.get("created_at"):
            row["created_at"] = existing["created_at"]   # keep, don't re-stamp
        if existing and not self._differs(existing, row, _INCIDENT_FIELDS):
            return False
        row["id"] = inc["id"]
        row["updated_at"] = time.time()
        cols = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        updates = ", ".join(f"{c}=excluded.{c}" for c in row if c != "id")
        self._exec(
            f"INSERT INTO incidents ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            tuple(row.values()))
        return True

    def delete_incident(self, inc_id: str, hard: bool = False) -> None:
        """Soft-delete by default so the removal reaches the other side."""
        if hard:
            self._exec("DELETE FROM incidents WHERE id=?", (inc_id,))
        else:
            self._exec(
                "UPDATE incidents SET deleted=1, updated_at=? WHERE id=?",
                (time.time(), inc_id))

    def incidents_since(self, ts: float) -> list[dict]:
        rows = self._query(
            "SELECT * FROM incidents WHERE COALESCE(updated_at, 0) > ? "
            "ORDER BY updated_at", (ts,))
        return [self._row_to_incident(r) for r in rows]

    @staticmethod
    def _incident_row(inc: dict) -> dict:
        return {
            "created_at": inc.get("created_at") or time.time(),
            "trigger": inc.get("trigger", "") or "",
            "transcript": json.dumps(inc.get("transcript", []),
                                     ensure_ascii=False),
            "world_name": inc.get("world_name", "") or "",
            "world_id": inc.get("world_id", "") or "",
            "instance_id": inc.get("instance_id", "") or "",
            "players": json.dumps(inc.get("players", []), ensure_ascii=False),
            "clip_path": inc.get("clip_path", "") or "",
            "screenshot_path": inc.get("screenshot_path", "") or "",
            "notes": inc.get("notes", "") or "",
            "status": inc.get("status", "new") or "new",
            "reported_by": inc.get("reported_by", "") or "",
            "origin": inc.get("origin", "") or "",
            "deleted": int(inc.get("deleted", 0) or 0),
        }

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
            "notes": r["notes"] or "", "status": r["status"] or "new",
            "reported_by": r["reported_by"] or "",
            "origin": r["origin"] or "",
            "updated_at": r["updated_at"] or 0.0,
            "deleted": bool(r["deleted"]),
        }

    # ---------------- age checks ----------------
    def all_age_checks(self, include_deleted: bool = False) -> list[dict]:
        sql = "SELECT * FROM age_checks"
        if not include_deleted:
            sql += " WHERE COALESCE(deleted, 0) = 0"
        sql += " ORDER BY created_at DESC"
        return [self._row_to_age_check(r) for r in self._query(sql)]

    def get_age_check(self, check_id: str) -> dict | None:
        r = self._one("SELECT * FROM age_checks WHERE id=?", (check_id,))
        return self._row_to_age_check(r) if r else None

    def age_checks_for_user(self, user_id: str) -> list[dict]:
        rows = self._query(
            "SELECT * FROM age_checks WHERE user_id=? AND COALESCE(deleted,0)=0"
            " ORDER BY created_at DESC", (user_id,))
        return [self._row_to_age_check(r) for r in rows]

    def upsert_age_check(self, check: dict) -> bool:
        row = self._age_check_row(check)
        existing = self._one("SELECT * FROM age_checks WHERE id=?",
                             (check["id"],))
        if existing and not check.get("created_at"):
            row["created_at"] = existing["created_at"]   # keep, don't re-stamp
        if existing and not self._differs(existing, row, _AGE_CHECK_FIELDS):
            return False
        row["id"] = check["id"]
        row["updated_at"] = time.time()
        cols = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        updates = ", ".join(f"{c}=excluded.{c}" for c in row if c != "id")
        self._exec(
            f"INSERT INTO age_checks ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            tuple(row.values()))
        return True

    def delete_age_check(self, check_id: str) -> None:
        self._exec("UPDATE age_checks SET deleted=1, updated_at=? WHERE id=?",
                   (time.time(), check_id))

    def age_checks_since(self, ts: float) -> list[dict]:
        rows = self._query(
            "SELECT * FROM age_checks WHERE COALESCE(updated_at, 0) > ? "
            "ORDER BY updated_at", (ts,))
        return [self._row_to_age_check(r) for r in rows]

    @staticmethod
    def _age_check_row(c: dict) -> dict:
        age = c.get("reported_age")
        return {
            "user_id": c.get("user_id", "") or "",
            "name": c.get("name", "") or "",
            "verdict": c.get("verdict", "") or "",
            "reported_age": int(age) if age not in (None, "") else None,
            "world_name": c.get("world_name", "") or "",
            "world_id": c.get("world_id", "") or "",
            "instance_id": c.get("instance_id", "") or "",
            "incident_id": c.get("incident_id", "") or "",
            "checked_by": c.get("checked_by", "") or "",
            "checked_by_id": c.get("checked_by_id", "") or "",
            "note": c.get("note", "") or "",
            "source": c.get("source", "") or "",
            "created_at": c.get("created_at") or time.time(),
            "deleted": int(c.get("deleted", 0) or 0),
        }

    @staticmethod
    def _row_to_age_check(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"], "user_id": r["user_id"] or "",
            "name": r["name"] or "", "verdict": r["verdict"] or "",
            "reported_age": r["reported_age"],
            "world_name": r["world_name"] or "",
            "world_id": r["world_id"] or "",
            "instance_id": r["instance_id"] or "",
            "incident_id": r["incident_id"] or "",
            "checked_by": r["checked_by"] or "",
            "checked_by_id": r["checked_by_id"] or "",
            "note": r["note"] or "", "source": r["source"] or "",
            "created_at": r["created_at"], "updated_at": r["updated_at"] or 0.0,
            "deleted": bool(r["deleted"]),
        }

    # ---------------- screening cache ----------------
    def all_users(self) -> dict:
        rows = self._query("SELECT * FROM screening_users")
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

    # ---------------- rosters ----------------
    def upsert_roster(self, client_id: str, snap: dict,
                      client_name: str = "") -> bool:
        """Record a heartbeat from a reporter. True if the roster changed.

        Reporters heartbeat every ~30s to prove they are alive, so bumping
        `updated_at` unconditionally would change state_version() on every
        heartbeat and reload every open browser twice a minute for nothing.
        Only a genuine join/leave (or world change) does that.
        """
        now = time.time()
        players = json.dumps(snap.get("players", []), ensure_ascii=False)
        world_name = snap.get("world_name", "") or ""
        world_id = snap.get("world_id", "") or ""
        instance_id = snap.get("instance_id", "") or ""

        existing = self._one("SELECT * FROM rosters WHERE client_id=?",
                             (client_id,))
        changed = (existing is None
                   or (existing["players"] or "[]") != players
                   or (existing["world_name"] or "") != world_name
                   or (existing["world_id"] or "") != world_id
                   or (existing["instance_id"] or "") != instance_id)
        self._exec(
            """INSERT INTO rosters
               (client_id, client_name, world_name, world_id, instance_id,
                players, updated_at, seen_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(client_id) DO UPDATE SET
                 client_name=excluded.client_name,
                 world_name=excluded.world_name, world_id=excluded.world_id,
                 instance_id=excluded.instance_id, players=excluded.players,
                 updated_at=CASE WHEN ? THEN excluded.updated_at
                                 ELSE rosters.updated_at END,
                 seen_at=excluded.seen_at""",
            (client_id, client_name, world_name, world_id, instance_id,
             players, now, now, 1 if changed else 0))
        return changed

    def all_rosters(self) -> list[dict]:
        # Most recently heard from first: a reporter still heartbeating beats
        # one whose last contact is older, even if the stale one changed later.
        rows = self._query("SELECT * FROM rosters "
                           "ORDER BY COALESCE(seen_at, updated_at) DESC")
        return [{
            "client_id": r["client_id"], "client_name": r["client_name"] or "",
            "world_name": r["world_name"] or "", "world_id": r["world_id"] or "",
            "instance_id": r["instance_id"] or "",
            "players": json.loads(r["players"] or "[]"),
            "updated_at": r["updated_at"] or 0.0,
            "seen_at": r["seen_at"] or r["updated_at"] or 0.0} for r in rows]

    # ---------------- web sessions ----------------
    # All of these take the SHA-256 of the session token, never the token
    # itself — see the schema comment and webapp/auth.py.
    def create_session(self, token_hash: str, user_id: str, name: str,
                       groups: list, ttl: float,
                       vrc_cookie: str = "") -> None:
        now = time.time()
        self._exec(
            "INSERT OR REPLACE INTO web_sessions "
            "(token, user_id, name, groups, created_at, expires_at, vrc_cookie) "
            "VALUES (?,?,?,?,?,?,?)",
            (token_hash, user_id, name,
             json.dumps(groups, ensure_ascii=False), now, now + ttl,
             vrc_cookie))

    def get_session(self, token_hash: str) -> dict | None:
        r = self._one("SELECT * FROM web_sessions WHERE token=?", (token_hash,))
        if not r:
            return None
        if (r["expires_at"] or 0) < time.time():
            self.delete_session(token_hash)
            return None
        return {"user_id": r["user_id"] or "", "name": r["name"] or "",
                "groups": json.loads(r["groups"] or "[]"),
                "created_at": r["created_at"], "expires_at": r["expires_at"],
                "vrc_cookie": r["vrc_cookie"] or ""}

    def set_session_cookie(self, token_hash: str, vrc_cookie: str) -> None:
        self._exec("UPDATE web_sessions SET vrc_cookie=? WHERE token=?",
                   (vrc_cookie, token_hash))

    def delete_session(self, token_hash: str) -> None:
        self._exec("DELETE FROM web_sessions WHERE token=?", (token_hash,))

    def purge_expired_sessions(self) -> None:
        self._exec("DELETE FROM web_sessions WHERE expires_at < ?",
                   (time.time(),))

    # ---------------- pending moderator actions ----------------
    def add_pending_action(self, rec: dict) -> bool:
        """Queue an audit-log action. False if we already had it."""
        if self._one("SELECT 1 FROM pending_actions WHERE id=?", (rec["id"],)):
            return False
        self._exec(
            """INSERT INTO pending_actions
               (id, group_id, action, actor_id, actor_name, target_id,
                target_name, location, created_at, noticed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (rec["id"], rec.get("group_id", ""), rec.get("action", "Kick"),
             rec.get("actor_id", ""), rec.get("actor_name", ""),
             rec.get("target_id", ""), rec.get("target_name", ""),
             rec.get("location", ""), rec.get("created_at") or time.time(),
             time.time()))
        return True

    def pending_actions(self, actor_id: str = "", include_done: bool = False
                        ) -> list[dict]:
        sql = "SELECT * FROM pending_actions WHERE 1=1"
        params: list = []
        if not include_done:
            sql += " AND resolved_at IS NULL AND COALESCE(dismissed, 0) = 0"
        if actor_id:
            sql += " AND actor_id = ?"
            params.append(actor_id)
        sql += " ORDER BY created_at DESC"
        return [dict(r) for r in self._query(sql, tuple(params))]

    def get_pending_action(self, action_id: str) -> dict | None:
        r = self._one("SELECT * FROM pending_actions WHERE id=?", (action_id,))
        return dict(r) if r else None

    def resolve_pending_action(self, action_id: str, reason: str,
                               incident_id: str) -> None:
        self._exec(
            "UPDATE pending_actions SET reason=?, incident_id=?, resolved_at=? "
            "WHERE id=?", (reason, incident_id, time.time(), action_id))

    def dismiss_pending_action(self, action_id: str) -> None:
        self._exec("UPDATE pending_actions SET dismissed=1 WHERE id=?",
                   (action_id,))

    def newest_audit_seen(self, group_id: str) -> float:
        r = self._one("SELECT MAX(created_at) AS t FROM pending_actions "
                      "WHERE group_id=?", (group_id,))
        return (r["t"] if r and r["t"] else 0.0)

    # ---------------- staff roster ----------------
    def all_staff(self) -> dict:
        rows = self._query("SELECT * FROM staff")
        return {r["user_id"]: {"name": r["name"] or "",
                               "role": r["role"] or "Mod",
                               "added_at": r["added_at"]} for r in rows}

    def upsert_staff(self, rec: dict) -> bool:
        uid = rec.get("user_id") or ""
        if not uid:
            return False
        name = rec.get("name", "") or ""
        role = rec.get("role", "Mod") or "Mod"
        existing = self._one("SELECT * FROM staff WHERE user_id=?", (uid,))
        if existing and (existing["name"] or "") == name \
                and (existing["role"] or "") == role:
            return False
        self._exec(
            "INSERT INTO staff (user_id, name, role, added_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, "
            "role=excluded.role, added_at=excluded.added_at",
            (uid, name, role, rec.get("added_at") or time.time()))
        return True

    # ---------------- change detection ----------------
    def state_version(self) -> str:
        """Cheap fingerprint of everything the web pages display.

        Polled by the browser every few seconds, so it must stay a handful of
        indexed MAX/COUNT lookups rather than reading any rows. Counts are in
        it too, so a hard delete changes the fingerprint even though it lowers
        no timestamp.
        """
        r = self._one("""
            SELECT (SELECT COALESCE(MAX(updated_at), 0) FROM incidents)  AS i,
                   (SELECT COALESCE(MAX(updated_at), 0) FROM age_checks) AS a,
                   (SELECT COALESCE(MAX(updated_at), 0) FROM rosters)    AS r,
                   (SELECT COUNT(*) FROM incidents)                      AS ic,
                   (SELECT COUNT(*) FROM age_checks)                     AS ac""")
        return (f"{r['i']:.3f}-{r['a']:.3f}-{r['r']:.3f}-{r['ic']}-{r['ac']}")

    # ---------------- sync cursors ----------------
    def sync_cursor(self, peer: str) -> dict:
        r = self._one("SELECT * FROM sync_state WHERE peer=?", (peer,))
        if not r:
            return {"last_push_at": 0.0, "last_pull_at": 0.0}
        return {"last_push_at": r["last_push_at"] or 0.0,
                "last_pull_at": r["last_pull_at"] or 0.0}

    def set_sync_cursor(self, peer: str, *, last_push_at: float | None = None,
                        last_pull_at: float | None = None) -> None:
        cur = self.sync_cursor(peer)
        self._exec(
            "INSERT INTO sync_state (peer, last_push_at, last_pull_at) "
            "VALUES (?,?,?) ON CONFLICT(peer) DO UPDATE SET "
            "last_push_at=excluded.last_push_at, "
            "last_pull_at=excluded.last_pull_at",
            (peer,
             cur["last_push_at"] if last_push_at is None else last_push_at,
             cur["last_pull_at"] if last_pull_at is None else last_pull_at))

    # ---------------- helpers ----------------
    @staticmethod
    def _differs(existing: sqlite3.Row, row: dict, fields: tuple) -> bool:
        """True if any tracked field changed. Timestamps compare loosely so a
        float round-trip through JSON doesn't register as an edit."""
        for f in fields:
            new, old = row[f], existing[f]
            if isinstance(new, float) or isinstance(old, float):
                if abs((new or 0) - (old or 0)) > 1e-6:
                    return True
            elif (new or "") != (old or ""):
                return True
        return False

    # ---------------- migrations ----------------
    def _migrate_columns(self) -> None:
        for table, cols in _ADDED_COLUMNS.items():
            have = {r["name"] for r in self._query(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    self._exec(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        # Pre-existing incidents have no updated_at; seed it from created_at so
        # a first sync sends real history instead of skipping it.
        self._exec("UPDATE incidents SET updated_at = COALESCE(created_at, 0) "
                   "WHERE updated_at IS NULL")
        # Created here rather than in _SCHEMA: the column it indexes only
        # exists after the ALTER TABLE above. Serves sync pulls and the
        # state_version() poll.
        self._exec("CREATE INDEX IF NOT EXISTS ix_incidents_updated "
                   "ON incidents(updated_at)")
        # Sessions from before tokens were hashed stored the raw token (43
        # chars) where a sha256 hex digest (64) now goes. They can never be
        # looked up again, so drop them rather than leave dead rows; the effect
        # is one forced sign-in at upgrade.
        self._exec("DELETE FROM web_sessions WHERE length(token) <> 64")

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
