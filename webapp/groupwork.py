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

#: VRChat rate limits group invites hard. A 429 is not a problem with the
#: action, just with how fast we asked, so it comes back sooner than a real
#: failure would.
RETRY_RATE_LIMITED = 120.0

#: Sent per pass *per usable session*, and how long to wait between sends.
#: VRChat rate limits per account, so two moderators signed in is genuinely
#: twice the throughput — the work is spread across them rather than piled on
#: whoever happened to be first in the dictionary.
BATCH = 5
BATCH_CAP = 40
SPACING = 2.0

#: An account that just answered 429 is stood down for a while and the work
#: goes to somebody else. Much better than parking the row: the backlog keeps
#: moving on every other session.
COOLING_FOR = 180.0

#: VRChat's way of saying the action was unnecessary. Not a failure: the person
#: is in the group, or has an invite sitting in their notifications, which is
#: the outcome we wanted. Retrying these forever would hammer the API for no
#: reason and leave the queue permanently dirty.
SETTLED = ("already a member", "already invited", "already in the group")

#: How long a 403 sidelines one moderator's session for one kind of action.
#: Not forever: group roles change, and a transient refusal should not cost us
#: that account until the process restarts.
DENIED_FOR = 600.0

#: No live session held the permission. Try again on the next poll rather than
#: waiting out RETRY_AFTER, because "somebody signed in" is the event we are
#: really waiting for and it can happen at any moment.
RETRY_NO_PROVIDER = 0.0


def _settled(error: Exception) -> bool:
    """Whether VRChat is telling us the action was already unnecessary."""
    text = _describe(error).lower()
    return any(phrase in text for phrase in SETTLED)


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
        #: Sessions VRChat has just rate limited, and when they went quiet.
        self._cooling: dict[str, float] = {}
        #: Where to start in the session list, so consecutive actions do not
        #: all land on the same account.
        self._turn = 0

    # ---------------- lifecycle ----------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def holding(self, kind: str) -> bool:
        """Whether this kind of action is queued but deliberately not sent.

        `hold_bans` / `hold_invites`. A brake worth having on a backlog of
        thousands: flip it and the queue stops sending without losing a row,
        which is the difference between pausing a mistake and living with it.
        """
        return bool(self.cfg.get(f"hold_{kind}s"))

    def usable(self, clients: dict, kind: str) -> list:
        """Sessions worth asking right now, in rotated order."""
        now = time.time()
        live = [(tok, api) for tok, api in clients.items()
                if now - self._denied.get((tok, kind), 0) >= DENIED_FOR
                and now - self._cooling.get(tok, 0) >= COOLING_FOR]
        if not live:
            return []
        start = self._turn % len(live)
        return live[start:] + live[:start]

    def status(self) -> dict:
        open_rows = self.db.group_actions(open_only=True, limit=500)
        return {"waiting": len(open_rows),
                "held": sum(1 for r in open_rows if self.holding(r["kind"])),
                "bans_held": bool(self.cfg.get("hold_bans")),
                "invites_held": bool(self.cfg.get("hold_invites")),
                "bans": sum(1 for r in open_rows if r["kind"] == "ban"),
                "invites": sum(1 for r in open_rows if r["kind"] == "invite"),
                "last_run": self.last_run,
                "last_error": self.last_error,
                "providers": len(self._live_clients()),
                "sending_with": len(self.usable(self._live_clients(), "invite"))}

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
        due = self.db.due_group_actions(limit=BATCH_CAP * 2)
        if not due:
            self.last_run = time.time()
            return 0

        clients = self._live_clients()
        # More sessions, more sending. One moderator's rate limit is not
        # everybody's.
        budget = min(BATCH_CAP, BATCH * max(1, len(clients)))
        done = 0
        for n, row in enumerate(due[:budget]):
            if self._stop.is_set():
                break
            if self.holding(row["kind"]):
                # Left exactly as it is: not attempted, not deferred, not
                # counted as a try. The row is the record of what would have
                # happened, and releasing the hold should find it untouched.
                continue
            if n and clients:
                self._stop.wait(SPACING)      # pace, so VRChat doesn't 429
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
        order = self.usable(clients, row["kind"])
        if not order:
            self.db.defer_group_action(
                row["id"],
                "every signed-in session is rate limited or lacks the "
                "permission", RETRY_RATE_LIMITED if self._cooling else
                RETRY_NO_PROVIDER)
            return False
        for token, api in order:
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
                # 429 is this account being told to slow down. Stand it down
                # and hand the same row straight to the next session, which
                # has its own budget.
                if "429" in text:
                    self._cooling[token] = time.time()
                    last_error = f"{who}: {text}"
                    continue
                # Anything else is about the action or VRChat itself, and
                # another moderator would hit exactly the same wall.
                wait = (RETRY_RATE_LIMITED if "429" in text else RETRY_AFTER)
                self.db.defer_group_action(row["id"], f"{who}: {text}", wait)
                self.last_error = f"{who}: {text}"
                return False
            self.db.finish_group_action(row["id"], who)
            self._turn += 1          # next action starts with the next account
            return True

        # Everybody available has now refused it.
        self.db.defer_group_action(
            row["id"], last_error,
            RETRY_RATE_LIMITED if "429" in last_error else RETRY_NO_PROVIDER)
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
            try:
                api.invite_to_group(row["group_id"], row["user_id"])
            except Exception as e:
                if _settled(e):
                    return          # already in, or already asked — that'll do
                raise
            return
        raise ValueError(f"unknown action {row['kind']!r}")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:               # never let the thread die
                self.last_error = f"{e}"
            self._stop.wait(self.interval)
