"""Minimal VRChat web-API client (login, 2FA, user lookup).

You log in with your own VRChat account from the Settings tab; the auth
cookie is stored locally in vrc_cookies.txt next to this file, so you only
need to re-enter credentials when the cookie expires. Credentials themselves
are never written to disk.

VRChat asks API clients to identify themselves with a descriptive User-Agent.
"""

import base64
import urllib.parse
from http.cookiejar import LWPCookieJar
from pathlib import Path

import requests

from paths import HERE

API = "https://api.vrchat.cloud/api/1"
APP_NAME = "VRC-ModTool"
APP_VERSION = "0.1"
COOKIE_PATH = HERE / "vrc_cookies.txt"


def build_user_agent(contact: str) -> str:
    """VRChat's WAF (403 / waf_code 13799) blocks requests unless the
    User-Agent names the app, a version, and REAL contact info — placeholders
    like 'example.com' are rejected. `contact` should be your own email/handle,
    kept in local config so it never lands in the public repo."""
    return f"{APP_NAME}/{APP_VERSION} {(contact or '').strip()}".strip()


def is_valid_contact(contact: str) -> bool:
    c = (contact or "").strip().lower()
    return bool(c) and "example.com" not in c and "your-contact" not in c


class VRChatAPIError(RuntimeError):
    pass


class VRChatAPI:
    """`cookie_path=None` keeps the auth cookie in memory only.

    The desktop app passes a path so you stay logged in between runs. The web
    server passes None: it holds one of these per signed-in moderator for the
    life of the process, so nobody else's VRChat session is ever written to
    disk on a shared machine.
    """

    def __init__(self, cookie_path: Path | None = COOKIE_PATH,
                 contact: str = ""):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = build_user_agent(contact)
        if cookie_path is not None:
            jar = LWPCookieJar(str(cookie_path))
            try:
                jar.load(ignore_discard=True)
            except OSError:
                pass
            self.s.cookies = jar
        self.user: dict | None = None

    def _save_cookies(self) -> None:
        if not isinstance(self.s.cookies, LWPCookieJar):
            return          # in-memory jar: nothing to persist
        try:
            self.s.cookies.save(ignore_discard=True)
        except OSError:
            pass

    # ---------------- auth ----------------
    def check_session(self) -> dict | None:
        """Return the logged-in user if the stored cookie is still valid."""
        r = self.s.get(f"{API}/auth/user", timeout=15)
        if r.ok and "requiresTwoFactorAuth" not in r.json():
            self.user = r.json()
            return self.user
        return None

    def login(self, username: str, password: str) -> str:
        """Start a login. Returns 'ok' or a required 2FA method
        ('totp' / 'emailotp')."""
        cred = (urllib.parse.quote(username, safe="")
                + ":" + urllib.parse.quote(password, safe=""))
        auth = base64.b64encode(cred.encode()).decode()
        r = self.s.get(f"{API}/auth/user",
                       headers={"Authorization": f"Basic {auth}"}, timeout=15)
        if r.status_code == 401:
            raise VRChatAPIError("Login failed: wrong username or password.")
        r.raise_for_status()
        j = r.json()
        self._save_cookies()
        methods = j.get("requiresTwoFactorAuth")
        if methods:
            if any(m.lower() == "totp" for m in methods):
                return "totp"
            return "emailotp"
        self.user = j
        return "ok"

    def verify_2fa(self, code: str, method: str) -> dict:
        r = self.s.post(f"{API}/auth/twofactorauth/{method}/verify",
                        json={"code": code.strip()}, timeout=15)
        if not r.ok:
            raise VRChatAPIError("2FA code rejected.")
        self._save_cookies()
        user = self.check_session()
        if not user:
            raise VRChatAPIError("2FA verified but session check failed.")
        return user

    def logout(self) -> None:
        try:
            self.s.put(f"{API}/logout", timeout=15)
        except requests.RequestException:
            pass
        self.s.cookies.clear()
        self._save_cookies()
        self.user = None

    # ---------------- lookups ----------------
    def search_users(self, query: str, n: int = 10) -> list[dict]:
        r = self.s.get(f"{API}/users",
                       params={"search": query, "n": n}, timeout=15)
        r.raise_for_status()
        return r.json()

    def get_user(self, user_id: str) -> dict:
        r = self.s.get(f"{API}/users/{user_id}", timeout=15)
        r.raise_for_status()
        return r.json()

    # ---------------- notes & groups (moderation) ----------------
    def list_user_notes(self) -> list[dict]:
        """Every note you've written, as {targetUserId, note, ...} objects.

        One call instead of one-per-user; we build a targetUserId->note map
        from it to tell which players are already tagged. The single-note
        variant is GET /userNotes/{userNoteId} (get_user_note)."""
        r = self.s.get(f"{API}/userNotes", timeout=15)
        r.raise_for_status()
        return r.json()

    def get_user_note(self, note_id: str) -> dict:
        r = self.s.get(f"{API}/userNotes/{note_id}", timeout=15)
        r.raise_for_status()
        return r.json()

    def get_user_groups(self, user_id: str) -> list[dict]:
        """Groups a user belongs to (each has name, groupId, shortCode, ...)."""
        r = self.s.get(f"{API}/users/{user_id}/groups", timeout=15)
        r.raise_for_status()
        return r.json()

    def get_public_profile(self, user_id: str) -> dict:
        """Public-facing profile: bio, pronouns, badges, trust tags.

        Separate from get_user(): this is what the profile page shows anyone,
        and it carries `pronouns` and VRChat's own `ageVerified` /
        `ageVerificationStatus`, which /users/{id} does not.
        """
        r = self.s.get(f"{API}/profile/{user_id}", timeout=15)
        r.raise_for_status()
        return r.json()

    def get_instance(self, world_id: str, instance_id: str) -> dict:
        """One instance as VRChat sees it: `n_users`, `capacity`, `ownerId`.

        VRChat will not tell you *who* is in an instance — that is why the
        roster agents exist — but it will tell you how many, and a headcount
        is enough to catch an agent describing a room it left.
        """
        r = self.s.get(f"{API}/instances/{world_id}:{instance_id}", timeout=20)
        r.raise_for_status()
        return r.json()

    def get_group_audit_logs(self, group_id: str, *, n: int = 60,
                             event_types: str = "", offset: int = 0,
                             start_date: str = "") -> dict:
        """Group audit log — who moderated whom, and when.

        Needs the `group-audit-view` permission on that group; without it
        VRChat answers 403 even though the group is visible to you. Instance
        kicks and warns appear as group.instance.kick / group.instance.warn,
        with the target's display name only inside the description text.
        """
        params: dict = {"n": n}
        if offset:
            params["offset"] = offset
        if event_types:
            params["eventTypes"] = event_types
        if start_date:
            params["startDate"] = start_date
        r = self.s.get(f"{API}/groups/{group_id}/auditLogs", params=params,
                       timeout=20)
        r.raise_for_status()
        return r.json()

    def group_member(self, group_id: str, user_id: str) -> dict | None:
        """This user's membership of a group, or None if they are not in it.

        404 is the ordinary "not a member" answer, not a failure — everything
        else is left to raise, so a permission problem or an outage is never
        mistaken for an absent member and acted on.
        """
        r = self.s.get(f"{API}/groups/{group_id}/members/{user_id}", timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def invite_to_group(self, group_id: str, user_id: str) -> dict:
        """Invite a user to a group. Needs `group-invites-manage`."""
        r = self.s.post(f"{API}/groups/{group_id}/invites",
                        json={"userId": user_id}, timeout=15)
        r.raise_for_status()
        return r.json() if r.content else {}

    def ban_from_group(self, group_id: str, user_id: str) -> dict:
        """Ban a user from a group. Needs `group-bans-manage`."""
        r = self.s.post(f"{API}/groups/{group_id}/bans",
                        json={"userId": user_id}, timeout=15)
        r.raise_for_status()
        return r.json() if r.content else {}

    def update_user_note(self, target_user_id: str, note: str) -> dict:
        """Set (replace) your private note on a user. Read the existing note
        first and merge if you want to append rather than overwrite."""
        r = self.s.post(f"{API}/userNotes",
                        json={"targetUserId": target_user_id, "note": note},
                        timeout=15)
        r.raise_for_status()
        return r.json()
