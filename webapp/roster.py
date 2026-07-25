"""Keep the instance roster current without needing the desktop app open.

The Screening page reads the `rosters` table. Originally only a desktop client
filled it, over the sync API — so closing the Tkinter app froze the page on
whatever roster was last pushed, which is worse than showing nothing: a
moderator can screen against a list of people who left half an hour ago.

When the server runs on the same machine as VRChat (the normal setup) it can
tail the output log itself, exactly as the desktop app does, and publish the
result. A remote desktop client pushing over sync still works; whichever
roster was updated most recently wins.
"""

import socket
import threading
import time

import vrc_log

#: Reserved client id for the roster this server reads itself.
LOCAL_CLIENT = "local-server"

#: A log untouched for longer than this means VRChat is closed or the session
#: ended — the roster is then history, not a live list.
LIVE_WINDOW = 300.0

#: Re-assert liveness this often even when nobody joins or leaves. Matches the
#: roster agent (agent.py) so both reporters rank comparably.
HEARTBEAT = 30.0


class LocalRosterPublisher:
    def __init__(self, database, interval: float = 3.0):
        self.db = database
        self.interval = interval
        self.watcher = vrc_log.VRCLogWatcher()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_revision = -1
        self.client_name = f"{socket.gethostname()} (server)"

    @staticmethod
    def available() -> bool:
        return vrc_log.LOG_DIR.exists()

    def start(self) -> None:
        self.watcher.start()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.watcher.stop()

    def log_age(self) -> float | None:
        """Seconds since VRChat last wrote to the log it is following."""
        path = self.watcher.log_path
        if not path:
            return None
        try:
            return time.time() - path.stat().st_mtime
        except OSError:
            return None

    def is_live(self) -> bool:
        age = self.log_age()
        return age is not None and age < LIVE_WINDOW

    def _loop(self) -> None:
        last_write = 0.0
        while not self._stop.is_set():
            try:
                snap = self.watcher.snapshot()
                # Publish only a real instance. An empty snapshot (no log yet,
                # or VRChat never started) must not overwrite a roster a remote
                # reporter legitimately pushed.
                due = (snap["revision"] != self._last_revision
                       or time.time() - last_write >= HEARTBEAT)
                if due and (snap["players"] or snap["world_id"]):
                    self._last_revision = snap["revision"]
                    last_write = time.time()
                    # Heartbeats keep this reporter ranked ahead of a remote
                    # agent that has gone quiet. upsert_roster only bumps
                    # updated_at on a real change, so they cost no page reloads.
                    self.db.upsert_roster(LOCAL_CLIENT, snap, self.client_name)
            except Exception:
                pass          # never let a bad read kill the publisher
            self._stop.wait(self.interval)
