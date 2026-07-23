"""Watch VRChat's local output log for world/instance and player events.

VRChat writes plain-text logs to  %USERPROFILE%\\AppData\\LocalLow\\VRChat\\VRChat.
This module tails the newest one and keeps a live picture of the current
instance: world name/id, instance id, and who is present right now.

The watcher parses the whole current log first (so state is correct even if
started mid-session), then follows new lines. When VRChat restarts and a newer
log file appears, the watcher switches to it automatically.
"""

import re
import threading
import time
from pathlib import Path

LOG_DIR = Path.home() / "AppData" / "LocalLow" / "VRChat" / "VRChat"

_RE_STAMP = re.compile(r"^(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})")
_RE_ROOM = re.compile(r"\[Behaviour\] Entering Room: (.+?)\s*$")
_RE_WORLD = re.compile(r"\[Behaviour\] Joining (wrld_[0-9a-f-]+):(\S+)")
_RE_JOIN = re.compile(r"\[Behaviour\] OnPlayerJoined (.+?)(?: \((usr_[0-9a-f-]+)\))?\s*$")
_RE_LEAVE = re.compile(r"\[Behaviour\] OnPlayerLeft (.+?)(?: \((usr_[0-9a-f-]+)\))?\s*$")


def _stamp_to_epoch(stamp: str) -> float:
    try:
        return time.mktime(time.strptime(stamp, "%Y.%m.%d %H:%M:%S"))
    except ValueError:
        return time.time()


def latest_log_file() -> Path | None:
    if not LOG_DIR.exists():
        return None
    logs = sorted(LOG_DIR.glob("output_log_*.txt"),
                  key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


class VRCLogWatcher:
    """Background thread that mirrors VRChat's instance state.

    Read state with snapshot(); `revision` increments on every change so the
    GUI can cheaply detect "something happened" without diffing.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.revision = 0
        self.log_path: Path | None = None
        self.world_name = ""
        self.world_id = ""
        self.instance_id = ""
        self.players: dict[str, dict] = {}   # key: user_id or name

    # ---------------- public API ----------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "revision": self.revision,
                "log_path": str(self.log_path) if self.log_path else "",
                "world_name": self.world_name,
                "world_id": self.world_id,
                "instance_id": self.instance_id,
                "players": [dict(p) for p in self.players.values()],
            }

    # ---------------- internals ----------------
    def _bump(self) -> None:
        self.revision += 1

    def _handle_line(self, line: str) -> None:
        m = _RE_STAMP.match(line)
        ts = _stamp_to_epoch(m.group(1)) if m else time.time()

        if (m := _RE_ROOM.search(line)):
            with self._lock:
                self.world_name = m.group(1)
                self._bump()
        elif (m := _RE_WORLD.search(line)):
            with self._lock:
                self.world_id = m.group(1)
                self.instance_id = m.group(2)
                self.players.clear()   # fresh instance, fresh roster
                self._bump()
        elif (m := _RE_JOIN.search(line)):
            name, uid = m.group(1), m.group(2) or ""
            with self._lock:
                self.players[uid or name] = {
                    "name": name, "user_id": uid, "joined_at": ts}
                self._bump()
        elif (m := _RE_LEAVE.search(line)):
            name, uid = m.group(1), m.group(2) or ""
            with self._lock:
                self.players.pop(uid or name, None)
                self._bump()

    def _run(self) -> None:
        fh = None
        last_scan = 0.0
        try:
            while not self._stop.is_set():
                # (re)check for a newer log file every few seconds
                if time.time() - last_scan > 5:
                    last_scan = time.time()
                    newest = latest_log_file()
                    if newest and newest != self.log_path:
                        if fh:
                            fh.close()
                        with self._lock:
                            self.log_path = newest
                            self.world_name = self.world_id = self.instance_id = ""
                            self.players.clear()
                            self._bump()
                        fh = open(newest, "r", encoding="utf-8",
                                  errors="replace")
                if fh is None:
                    time.sleep(2)
                    continue
                line = fh.readline()
                if line:
                    self._handle_line(line)
                else:
                    time.sleep(1.0)
        finally:
            if fh:
                fh.close()
