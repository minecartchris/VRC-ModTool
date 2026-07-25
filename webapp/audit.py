"""Watch the VRChat group audit log for kicks and warns that need a reason.

VRChat records every instance kick and warn against the group, but not *why*.
This polls that log and queues each action so the moderator who issued it is
asked for a reason while they still remember it — instead of the log being
reconstructed from memory hours later, or never.

    group.instance.kick   ->  Kick
    group.instance.warn   ->  Warn

Polling borrows the live VRChat session of a signed-in moderator, so no extra
account or stored credential is needed. It requires the `group-audit-view`
permission on the watched group: without it VRChat answers 403 for everyone,
and status() reports that rather than failing silently.
"""

import re
import threading
import time
from datetime import datetime, timezone

EVENT_ACTIONS = {
    "group.instance.kick": "Kick",
    "group.instance.warn": "Warn",
}

#: "MaeMardis2 has issued an instance kick for CollzToons." — the target's
#: display name appears nowhere else in the entry, only its user id.
_TARGET = re.compile(r"\bfor\s+(.+?)\.?\s*$")

#: Don't queue history on first run; only actions from now on.
BACKFILL_SECONDS = 3600.0


def parse_ts(value) -> float:
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return time.time()


def target_name(entry: dict) -> str:
    match = _TARGET.search(str(entry.get("description") or ""))
    if match:
        return match.group(1).strip()
    return entry.get("targetId", "") or "(unknown)"


class AuditWatcher:
    def __init__(self, database, cfg: dict, sessions, interval: float = 60.0):
        self.db = database
        self.cfg = cfg
        self.sessions = sessions
        self.interval = interval
        self.group_id = (cfg.get("audit_group") or "").strip()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self.last_poll = 0.0
        self.last_ok = 0.0
        self.queued = 0
        #: Whose permissions are currently making this work, shown on /pending
        #: so it's obvious the feature depends on them staying signed in.
        self.provider = ""
        self._last_good_token = ""

    @property
    def configured(self) -> bool:
        return bool(self.group_id)

    def start(self) -> None:
        if not self.configured:
            self.last_error = "no audit_group configured"
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {"configured": self.configured, "group_id": self.group_id,
                "last_poll": self.last_poll, "last_ok": self.last_ok,
                "error": self.last_error, "queued": self.queued,
                "provider": self.provider}

    def poll_once(self) -> int:
        """Fetch recent kicks/warns and queue any we haven't seen. Returns the
        number newly queued."""
        page = self._fetch_with_any_session()
        if page is None:
            return 0

        # First run: only look back a little, so turning this on doesn't
        # confront somebody with a backlog of every kick the group ever had.
        watermark = self.db.newest_audit_seen(self.group_id)
        if not watermark:
            watermark = time.time() - BACKFILL_SECONDS

        added = 0
        for entry in page.get("results", []):
            action = EVENT_ACTIONS.get(entry.get("eventType"))
            if not action:
                continue
            created = parse_ts(entry.get("created_at"))
            if created <= watermark:
                continue
            if self.db.add_pending_action({
                    "id": entry.get("id"), "group_id": self.group_id,
                    "action": action,
                    "actor_id": entry.get("actorId", ""),
                    "actor_name": entry.get("actorDisplayName", ""),
                    "target_id": entry.get("targetId", ""),
                    "target_name": target_name(entry),
                    "location": (entry.get("data") or {}).get("location", ""),
                    "created_at": created}):
                added += 1

        self.last_ok = time.time()
        self.last_error = ""
        self.queued += added
        return added

    def _fetch_with_any_session(self) -> dict | None:
        """Try every signed-in moderator's own VRChat session in turn.

        Audit access is a per-account permission, so the tool should work as
        long as *somebody* who holds it is logged in — typically senior staff.
        Everyone else's 403 is skipped rather than treated as failure. The
        account that worked is remembered and tried first next time, so the
        steady state is one API call per poll.
        """
        with self.sessions._lock:                     # noqa: SLF001
            clients = dict(self.sessions._clients)
        if not clients:
            self.provider = ""
            self.last_error = ("nobody is signed in — the audit log is read "
                               "with a moderator's own VRChat permissions, so "
                               "someone holding group-audit-view must be "
                               "logged in")
            return None

        ordered = sorted(clients.items(),
                         key=lambda kv: kv[0] != self._last_good_token)
        denied = 0
        for token, api in ordered:
            try:
                page = api.get_group_audit_logs(
                    self.group_id, n=60, event_types=",".join(EVENT_ACTIONS))
            except Exception as e:
                if "403" in str(e):
                    denied += 1
                    continue                 # this account just lacks the perm
                self.last_error = f"audit poll failed: {e}"
                return None
            self._last_good_token = token
            self.provider = getattr(api, "user", {}).get("displayName", "") \
                if isinstance(getattr(api, "user", None), dict) else ""
            return page

        self.provider = ""
        self.last_error = (
            f"none of the {denied} signed-in moderator(s) hold the "
            f"group-audit-view permission on this group, so VRChat refuses "
            f"the audit log. Someone senior enough needs to sign in.")
        return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.last_poll = time.time()
                self.poll_once()
            except Exception as e:                    # never kill the thread
                self.last_error = f"{type(e).__name__}: {e}"
            self._stop.wait(self.interval)
