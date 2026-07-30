"""Bans and invites, carried out with a signed-in moderator's permissions.

The server has no VRChat account of its own. Everything it does inside VRChat
is done through a moderator who is signed in here, exactly as the audit-log
watcher reads the log with whoever happens to hold `group-audit-view`.

That makes timing the hard part. A kick for being overage happens at 3am; the
ban that should follow needs somebody holding `group-bans-manage` with a live
session. So actions are queued in the database and this thread drains them
whenever such a session exists — the moment one signs in, the backlog goes out.
Nothing is dropped and nothing expires: a failure is rescheduled, and the row
keeps who asked for it and why.

Ordering note: a queued action is *not* a promise that VRChat will accept it.
The person may have left the group, or been banned already. Those come back as
errors, get recorded on the row, and are visible on the Admin page rather than
disappearing quietly.
"""

import threading
import time

#: How often to look for work. Short enough that an action raised while a
#: moderator is signed in goes out while they are still there.
POLL = 20.0

#: A VRChat call that failed for a reason retrying might fix — rate limiting,
#: an outage, a stale cookie. Long enough not to hammer them.
RETRY_AFTER = 900.0

#: How long a 403 sidelines one moderator's session for one kind of action.
#: Not forever: group roles change, and a transient refusal should not cost us
#: that account until the process restarts.
DENIED_FOR = 600.0

#: No live session held the permission. Try again on the next poll rather than
#: waiting out RETRY_AFTER, because "somebody signed in" is the event we are
#: really waiting for and it can happen at any moment.
RETRY_NO_PROVIDER = 0.0


def _describe(error: Exception) -> str:
    """VRChat's own words, not just the status line.

    requests puts the URL in the message and the reason in the body, and the
    body is the half that says *which* permission was missing.
    """
    body = ""
    resp = getattr(error, "response", None)
    if resp is not None:
        body = (getattr(resp, "text", "") or "").strip()[:180]
    return f"{error}" + (f" — {body}" if body else "")


class GroupWorker:
    def __init__(self, database, cfg: dict, sessions, interval: float = POLL):
        self.db = database
        self.cfg = cfg
        self.sessions = sessions
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run = 0.0
        self.last_error = ""
        #: Sessions that answered 403 for a kind of action, so we stop asking
        #: them every 20 seconds. Cleared whenever the process restarts, which
        #: is also when someone's permissions might have changed.
        self._denied: dict[tuple[str, str], float] = {}

    # ---------------- lifecycle ----------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        open_rows = self.db.group_actions(open_only=True, limit=500)
        return {"waiting": len(open_rows),
                "bans": sum(1 for r in open_rows if r["kind"] == "ban"),
                "invites": sum(1 for r in open_rows if r["kind"] == "invite"),
                "last_run": self.last_run,
                "last_error": self.last_error,
                "providers": len(self._live_clients())}

    # ---------------- the work ----------------
    def _live_clients(self) -> dict:
        """Moderators whose VRChat session this process can currently use.

        Only sessions that have been used since the last restart are here —
        the stored cookie is encrypted with the browser's token, so it can be
        rehydrated on a request but never conjured up in the background.
        """
        with self.sessions._lock:
            return dict(self.sessions._clients)

    def run_once(self) -> int:
        """Attempt every action that is due. Returns how many went out."""
        due = self.db.due_group_actions()
        if not due:
            self.last_run = time.time()
            return 0

        clients = self._live_clients()
        done = 0
        for row in due:
            if self._stop.is_set():
                break
            if not clients:
                self.db.defer_group_action(
                    row["id"], "waiting for a moderator with the permission "
                                "to sign in", RETRY_NO_PROVIDER)
                continue
            done += 1 if self._attempt(row, clients) else 0
        self.last_run = time.time()
        return done

    def _attempt(self, row: dict, clients: dict) -> bool:
        last_error = "no signed-in moderator holds that permission"
        now = time.time()
        for token, api in clients.items():
            # Denials expire: somebody's group role can change while the
            # process is up, and a 403 that turns out to have been transient
            # should not sideline that account until the next restart.
            denied_at = self._denied.get((token, row["kind"]), 0)
            if denied_at and now - denied_at < DENIED_FOR:
                continue
            who = (getattr(api, "user", None) or {}).get("displayName", "?")
            try:
                self._perform(api, row)
            except Exception as e:
                text = _describe(e)
                # 403 is this account lacking the permission, not a failure of
                # the action — try the next moderator, and stop asking this one.
                if "403" in text:
                    self._denied[(token, row["kind"])] = time.time()
                    last_error = f"{who}: {text}"
                    continue
                # Anything else is about the action or VRChat itself, and
                # another moderator would hit exactly the same wall.
                self.db.defer_group_action(row["id"], f"{who}: {text}",
                                           RETRY_AFTER)
                self.last_error = f"{who}: {text}"
                return False
            self.db.finish_group_action(row["id"], who)
            return True

        self.db.defer_group_action(row["id"], last_error, RETRY_NO_PROVIDER)
        return False

    def _perform(self, api, row: dict) -> None:
        if row["kind"] == "ban":
            api.ban_from_group(row["group_id"], row["user_id"])
            return
        if row["kind"] == "invite":
            # Checked at send time, not at queue time: between a verdict and a
            # moderator being available the person may well have joined, and
            # inviting an existing member is noise for them.
            #
            # Reading the member list is a *different* permission from sending
            # an invite, so a moderator who can invite may well be refused
            # here. Failing the whole action on that would be the tail wagging
            # the dog: if we cannot tell, send it. A duplicate invite is a
            # smaller problem than never inviting anybody.
            try:
                if api.group_member(row["group_id"], row["user_id"]):
                    return
            except Exception:
                pass
            api.invite_to_group(row["group_id"], row["user_id"])
            return
        raise ValueError(f"unknown action {row['kind']!r}")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:               # never let the thread die
                self.last_error = f"{e}"
            self._stop.wait(self.interval)
