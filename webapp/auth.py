"""Sign-in by VRChat account, authorised by staff-group membership.

There is no separate password for this app. A moderator signs in with their own
VRChat credentials; the server forwards them to VRChat, and if the account
comes back as a member of the configured staff group it gets a session. That
means the moderator list *is* the group roster — remove someone from the group
in VRChat and they lose access here the next time they sign in.

What the server keeps, both in modtool.db:
  * the SHA-256 of the session token, their user id/name, and which staff
    groups matched;
  * their VRChat auth cookie, encrypted with a key derived from the raw
    session token.

The raw token lives only in the browser cookie, so the database on its own
decrypts nothing — someone who copies modtool.db gets neither a usable session
nor anyone's VRChat login. Persisting it at all is what lets the server restart
(on a code change, say) without everyone having to sign in again.

What it never keeps: the password. It is used for the one upstream call and
then dropped.
"""

import base64
import hashlib
import json
import secrets
import threading
import time

import requests
from cryptography.fernet import Fernet, InvalidToken

import vrc_api

PENDING_TTL = 300.0        # seconds a half-finished 2FA login stays valid
_MAX_ATTEMPTS = 8          # per IP, per window — we are proxying to VRChat
_WINDOW = 300.0


class AuthError(RuntimeError):
    pass


def token_hash(token: str) -> str:
    """What goes in the database in place of the session token."""
    return hashlib.sha256(token.encode()).hexdigest()


def _cookie_key(token: str) -> bytes:
    """Fernet key derived from the raw session token.

    A separate digest from token_hash, so the value stored in the database is
    not itself the encryption key. The token already carries 256 bits of
    entropy from secrets.token_urlsafe, so one hash round is enough — this is
    key separation, not password stretching.
    """
    digest = hashlib.sha256(b"modsuite-cookie-v1|" + token.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_cookies(token: str, jar) -> str:
    """Freeze a requests cookie jar into an encrypted blob."""
    try:
        data = json.dumps(requests.utils.dict_from_cookiejar(jar))
        return Fernet(_cookie_key(token)).encrypt(data.encode()).decode()
    except Exception:
        return ""


def decrypt_cookies(token: str, blob: str) -> dict | None:
    if not blob:
        return None
    try:
        raw = Fernet(_cookie_key(token)).decrypt(blob.encode())
        return json.loads(raw.decode())
    except (InvalidToken, ValueError, TypeError):
        return None


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

    def is_admin(self, user_id: str) -> bool:
        """Admins from the config and from the table, same as the server's own
        check — kept here too because sign-in happens before any of that."""
        if not user_id:
            return False
        roots = {str(r).strip() for r in (self.cfg.get("root_admins") or [])}
        if user_id in roots:
            return True
        try:
            return bool(self.db.is_admin(user_id))
        except Exception:
            return False        # a database hiccup must not grant access

    def _finish(self, api: vrc_api.VRChatAPI) -> dict:
        """Verify staff membership, then mint a session.

        Two ways in: the staff group, or the admin list. Everyone else is
        refused."""
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
        if not matched and self.is_admin(uid):
            # Being on the admin list is its own way in. An admin who has left
            # the staff group, or whose group read came back short, should not
            # be locked out of the tool they administer.
            #
            # The cost is that removing somebody from the staff group no longer
            # removes their access on its own — they have to come off the admin
            # list too. That is why this is announced in the log and printed on
            # the page rather than being a silent exception.
            matched = ["the admin list"]
            print(f"[auth] {name} ({uid}) signed in from the admin list, "
                  f"not the staff group", flush=True)
        if not matched:
            raise AuthError(f"{name} is not in the staff group and is not on "
                            f"the admin list. Access denied.")

        token = secrets.token_urlsafe(32)
        ttl = float(self.cfg.get("session_hours", 12)) * 3600
        # Remembered beyond the session, so somebody can be made an admin
        # months after their last sign-in rather than only while logged in.
        self.db.note_known_user(uid, name)
        self.db.purge_expired_sessions()
        self.db.create_session(token_hash(token), uid, name, matched, ttl,
                               encrypt_cookies(token, api.s.cookies))
        with self._lock:
            self._clients[token] = api
        return {"token": token, "user_id": uid, "name": name,
                "groups": matched}

    # ---------------- session access ----------------
    def get(self, token: str | None) -> dict | None:
        if not token:
            return None
        return self.db.get_session(token_hash(token))

    def client(self, token: str | None) -> vrc_api.VRChatAPI | None:
        """Live VRChat client for a session.

        Rebuilt from the stored cookie when this process doesn't have one yet,
        which is what carries a signed-in moderator across a server restart.
        Only the browser's raw token can decrypt it, so the rehydration has to
        happen here on a request rather than at startup.
        """
        if not token:
            return None
        with self._lock:
            api = self._clients.get(token)
        if api:
            return api

        sess = self.db.get_session(token_hash(token))
        if not sess:
            return None
        cookies = decrypt_cookies(token, sess.get("vrc_cookie", ""))
        if not cookies:
            return None
        try:
            api = self._new_api()
        except AuthError:
            return None
        api.s.cookies = requests.utils.cookiejar_from_dict(cookies)
        # Trust the cookie rather than round-tripping to VRChat on a page
        # render; a stale one surfaces as an error on the first note write.
        api.user = {"id": sess["user_id"], "displayName": sess["name"]}
        with self._lock:
            self._clients[token] = api
        return api

    def refresh_stored_cookies(self, token: str) -> None:
        """Re-save after VRChat rotates the cookie mid-session."""
        api = self.client(token)
        if api:
            self.db.set_session_cookie(
                token_hash(token), encrypt_cookies(token, api.s.cookies))

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
        self.db.delete_session(token_hash(token))

    def _prune_pending(self) -> None:
        now = time.time()
        for k, v in list(self._pending.items()):
            if now - v["at"] > PENDING_TTL:
                del self._pending[k]
