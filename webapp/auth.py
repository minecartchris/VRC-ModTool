"""Sign-in by VRChat account, authorised by staff-group membership.

There is no separate password for this app. A moderator signs in with their own
VRChat credentials; the server forwards them to VRChat, and if the account
comes back as a member of the configured staff group it gets a session. That
means the moderator list *is* the group roster — remove someone from the group
in VRChat and they lose access here the next time they sign in.

What the server keeps:
  * in memory, for the life of the process — the VRChat auth cookie, so the
    signed-in moderator can write user notes;
  * in modtool.db — an opaque session token, their user id/name, and which
    staff groups matched.

What it never keeps: the password. It is used for the one upstream call and
then dropped.
"""

import secrets
import threading
import time

import vrc_api

PENDING_TTL = 300.0        # seconds a half-finished 2FA login stays valid
_MAX_ATTEMPTS = 8          # per IP, per window — we are proxying to VRChat
_WINDOW = 300.0


class AuthError(RuntimeError):
    pass


def staff_groups(groups: list[dict], staff_group: str) -> list[str]:
    """Names of the user's groups matching the configured staff group.

    Matches a full group id (grp_...), a short code, or a case-insensitive
    substring of the name, so the setting can be whichever is handy.
    """
    want = (staff_group or "").strip().lower()
    if not want:
        return []
    out = []
    for g in groups:
        gid = (g.get("groupId") or g.get("id") or "").lower()
        code = (g.get("shortCode") or g.get("code") or "").lower()
        name = (g.get("name") or "").lower()
        if want == gid or want == code or (want in name and name):
            out.append(g.get("name") or g.get("groupId") or g.get("id") or "?")
    return out


class SessionManager:
    """Owns pending 2FA attempts, live VRChat clients, and login throttling."""

    def __init__(self, database, cfg: dict):
        self.db = database
        self.cfg = cfg
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}      # ticket -> {api, method, at}
        self._clients: dict[str, vrc_api.VRChatAPI] = {}  # session -> api
        self._attempts: dict[str, list[float]] = {}       # ip -> timestamps

    # ---------------- throttle ----------------
    def check_rate(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            hits = [t for t in self._attempts.get(ip, []) if now - t < _WINDOW]
            if len(hits) >= _MAX_ATTEMPTS:
                raise AuthError("Too many sign-in attempts. Wait a few minutes "
                                "and try again.")
            hits.append(now)
            self._attempts[ip] = hits

    # ---------------- login ----------------
    def _new_api(self) -> vrc_api.VRChatAPI:
        contact = (self.cfg.get("vrc_contact") or "").strip()
        if not vrc_api.is_valid_contact(contact):
            raise AuthError(
                "Server is missing its VRChat API contact. Set \"vrc_contact\" "
                "in web_config.json to a real email or Discord handle — "
                "VRChat rejects API calls without one.")
        return vrc_api.VRChatAPI(cookie_path=None, contact=contact)

    def begin_login(self, username: str, password: str) -> dict:
        """Returns {'status': 'ok', 'session': ...} or
        {'status': '2fa', 'ticket': ..., 'method': ...}."""
        api = self._new_api()
        try:
            result = api.login(username, password)
        except vrc_api.VRChatAPIError as e:
            raise AuthError(str(e)) from e
        except Exception as e:                      # network / HTTP failure
            raise AuthError(f"Could not reach VRChat: {e}") from e

        if result == "ok":
            return {"status": "ok", "session": self._finish(api)}
        ticket = secrets.token_urlsafe(24)
        with self._lock:
            self._prune_pending()
            self._pending[ticket] = {"api": api, "method": result,
                                     "at": time.time()}
        return {"status": "2fa", "ticket": ticket, "method": result}

    def complete_2fa(self, ticket: str, code: str) -> dict:
        with self._lock:
            self._prune_pending()
            entry = self._pending.pop(ticket, None)
        if not entry:
            raise AuthError("That sign-in expired. Start again.")
        api, method = entry["api"], entry["method"]
        try:
            api.verify_2fa(code, method)
        except vrc_api.VRChatAPIError as e:
            # Put it back so a typo doesn't cost a full re-login.
            with self._lock:
                self._pending[ticket] = entry
            raise AuthError(str(e)) from e
        except Exception as e:
            raise AuthError(f"Could not reach VRChat: {e}") from e
        return self._finish(api)

    def _finish(self, api: vrc_api.VRChatAPI) -> dict:
        """Verify staff membership, then mint a session."""
        user = api.user or api.check_session()
        if not user:
            raise AuthError("VRChat accepted the login but returned no user.")
        uid = user.get("id", "")
        name = user.get("displayName") or user.get("username") or uid

        want = (self.cfg.get("staff_group") or "").strip()
        if not want:
            raise AuthError(
                "No staff group is configured, so nobody can sign in. Set "
                "\"staff_group\" in web_config.json to your moderator group's "
                "ID, short code, or name.")
        try:
            groups = api.get_user_groups(uid)
        except Exception as e:
            raise AuthError(f"Signed in, but couldn't read your groups: {e}"
                            ) from e
        matched = staff_groups(groups, want)
        if not matched:
            raise AuthError(f"{name} is not in the staff group. Access denied.")

        token = secrets.token_urlsafe(32)
        ttl = float(self.cfg.get("session_hours", 12)) * 3600
        self.db.purge_expired_sessions()
        self.db.create_session(token, uid, name, matched, ttl)
        with self._lock:
            self._clients[token] = api
        return {"token": token, "user_id": uid, "name": name,
                "groups": matched}

    # ---------------- session access ----------------
    def get(self, token: str | None) -> dict | None:
        if not token:
            return None
        return self.db.get_session(token)

    def client(self, token: str | None) -> vrc_api.VRChatAPI | None:
        """Live VRChat client for a session, if this process still holds one.

        Absent after a server restart: the DB session is still valid for
        reading records, but note-writing needs a fresh sign-in.
        """
        with self._lock:
            return self._clients.get(token or "")

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            api = self._clients.pop(token, None)
        if api:
            try:
                api.logout()
            except Exception:
                pass
        self.db.delete_session(token)

    def _prune_pending(self) -> None:
        now = time.time()
        for k, v in list(self._pending.items()):
            if now - v["at"] > PENDING_TTL:
                del self._pending[k]
