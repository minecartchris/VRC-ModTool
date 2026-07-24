"""Desktop → server sync for incidents and age checks.

The desktop app owns the capture side (audio triggers, Medal clips, VRChat log)
and keeps working with no server at all. When a server URL and token are set on
the Settings tab, this pushes local records up and pulls remote ones down on a
timer, so a check filed from someone's phone shows up in the Tkinter list and
vice versa.

Conflict handling is last-write-wins on content. That is safe here because the
two sides edit different things in practice — the desktop writes new incidents
and clip paths, the web writes notes, statuses and age checks — and because
db.upsert_* only bumps `updated_at` when content actually changes, so a record
that round-trips does not bounce back and forth forever.
"""

import threading
import time
import uuid

import requests

PEER = "server"
DEFAULT_INTERVAL = 60.0
_TIMEOUT = 20


class SyncError(RuntimeError):
    pass


def new_client_id() -> str:
    return uuid.uuid4().hex[:12]


class SyncClient:
    def __init__(self, database, url: str, token: str, client_id: str = "",
                 client_name: str = ""):
        self.db = database
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.client_id = client_id or new_client_id()
        self.client_name = client_name
        self.last_error = ""
        self.last_sync_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._roster: dict | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    # ---------------- one round ----------------
    def sync_once(self, roster: dict | None = None) -> dict:
        """Pull, then push. Returns counts; raises SyncError on failure."""
        if not self.configured:
            raise SyncError("No server URL or token set.")
        with self._lock:
            stats = {"pulled": 0, "pushed": 0}
            stats["pulled"] = self._pull()
            stats["pushed"] = self._push(roster)
            self.last_sync_at = time.time()
            self.last_error = ""
            return stats

    def _headers(self) -> dict:
        return {"X-Sync-Token": self.token}

    def _pull(self) -> int:
        cursor = self.db.sync_cursor(PEER)
        try:
            r = requests.get(f"{self.url}/api/sync/pull",
                             params={"since": cursor["last_pull_at"]},
                             headers=self._headers(), timeout=_TIMEOUT)
        except requests.RequestException as e:
            raise SyncError(f"Can't reach server: {e}") from e
        self._check(r)
        data = r.json()
        applied = 0
        for inc in data.get("incidents", []):
            if self.db.upsert_incident(inc):
                applied += 1
        for chk in data.get("age_checks", []):
            if self.db.upsert_age_check(chk):
                applied += 1
        self.db.set_sync_cursor(
            PEER, last_pull_at=float(data.get("watermark",
                                              cursor["last_pull_at"])))
        return applied

    def _push(self, roster: dict | None) -> int:
        cursor = self.db.sync_cursor(PEER)
        since = cursor["last_push_at"]
        incidents = self.db.incidents_since(since)
        checks = self.db.age_checks_since(since)
        payload = {
            "client_id": self.client_id,
            "client_name": self.client_name,
            "incidents": incidents,
            "age_checks": checks,
        }
        if roster:
            payload["roster"] = roster
        if not (incidents or checks or roster):
            return 0
        try:
            r = requests.post(f"{self.url}/api/sync/push", json=payload,
                              headers=self._headers(), timeout=_TIMEOUT)
        except requests.RequestException as e:
            raise SyncError(f"Can't reach server: {e}") from e
        self._check(r)
        # Advance to the newest record we actually sent, not to "now": a write
        # that lands mid-request keeps a timestamp above the cursor and goes
        # out on the next round instead of being skipped.
        newest = max([since]
                     + [i["updated_at"] for i in incidents]
                     + [c["updated_at"] for c in checks])
        self.db.set_sync_cursor(PEER, last_push_at=newest)
        return len(incidents) + len(checks)

    @staticmethod
    def _check(r: requests.Response) -> None:
        if r.status_code == 401:
            raise SyncError("Server rejected the sync token.")
        if r.status_code == 503:
            raise SyncError("Server has sync disabled (no sync_token set).")
        if not r.ok:
            raise SyncError(f"Server error {r.status_code}: {r.text[:200]}")

    # ---------------- background loop ----------------
    def start(self, interval: float = DEFAULT_INTERVAL,
              roster_fn=None, on_result=None) -> None:
        """Sync every `interval` seconds until stop().

        roster_fn  — called each round for the current instance snapshot
        on_result  — called with (stats, error) after each round; the GUI uses
                     it to update the status line from its own thread queue
        """
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                try:
                    stats = self.sync_once(roster_fn() if roster_fn else None)
                    if on_result:
                        on_result(stats, "")
                except SyncError as e:
                    self.last_error = str(e)
                    if on_result:
                        on_result(None, str(e))
                except Exception as e:                      # never kill the thread
                    self.last_error = f"{type(e).__name__}: {e}"
                    if on_result:
                        on_result(None, self.last_error)
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive()
                    and not self._stop.is_set())
