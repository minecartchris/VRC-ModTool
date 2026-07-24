"""VRChat moderation suite GUI. Run with gui.bat (or pythonw gui.py).

Tabs:
    Listener  — the original auto-clipper: capture, transcribe, trigger.
    Instance  — live view of the current VRChat world and player roster.
    Screening — age-check workflow: per-player Over / Under / In Range, with
                note tags and group flags pulled from the VRChat API.
    Incidents — every trigger becomes an incident: transcript, roster, clip.
    Settings  — in-VR notifications, Medal folder, VRChat API login.
"""

import argparse
import ctypes
import json
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

import autoclip
import capture
import db
import incidents
import notify
import report
import vrc_log

try:  # crisp UI + correct coordinates on scaled displays
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

BG = "#1e1e1e"
PANEL = "#252526"
FG = "#d4d4d4"
DIM = "#8a8a8a"
GREEN = "#4ec9b0"
RED = "#f14c4c"
YELLOW = "#dcdcaa"
BLUE = "#569cd6"
FONT = ("Segoe UI", 10)
MONO = ("Consolas", 10)

# Captures both VRChat/game audio (Wave Link "System") and Discord voices.
VRCHAT_DISCORD_PRESET = "VRChat + Discord (System + Discord)"
VRCHAT_DISCORD_FILTERS = ["System (Elgato", "Discord (Elgato"]

CONFIG_PATH = autoclip.HERE / "config.json"
DEFAULT_CONFIG = {
    "device": VRCHAT_DISCORD_PRESET,
    "model": "",
    "hotkey": autoclip.MEDAL_HOTKEY,
    "cooldown": autoclip.COOLDOWN_SECONDS,
    "xsoverlay": True,
    "chatbox": False,
    "medal_dir": str(incidents.DEFAULT_MEDAL_DIR),
    # Screening tab: the tag written to / matched in user notes, and an
    # optional group-name substring that flags members of a watched group.
    "note_filter": "age ok",
    "group_filter": "",
    # VRChat requires REAL contact info in the API User-Agent (email/handle).
    # Kept here in local config so it never ends up in the public repo.
    "vrc_contact": "",
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


class App:
    def __init__(self, root: tk.Tk, args: argparse.Namespace):
        self.root = root
        root.title("VRChat Mod Suite")
        root.configure(bg=BG)
        root.geometry("1120x680")
        root.minsize(900, 520)

        self.cfg = load_config()
        self.logfile = open(autoclip.HERE / "session.log", "a",
                            encoding="utf-8", buffering=1)
        self.logfile.write(f"\n=== session started "
                           f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")

        self.q: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.running = False
        self.started_at = 0.0
        self.heard_count = 0
        self.trigger_count = 0

        self.db = db.Database()
        self.store = incidents.IncidentStore(self.db)
        self.heard_buffer: deque[str] = deque(maxlen=6)
        self.pending_incident: tuple[str, float, int] | None = None  # id, ts, lines left

        self.watcher = vrc_log.VRCLogWatcher()
        self.watcher.start()
        self._vrc_rev = -1

        self.vrc_api = None            # created lazily on the settings tab
        self._2fa_method = ""
        self.scr_rows: dict[str, dict] = {}   # iid -> current on-screen row
        self.scr_db = self.db.all_users()     # user_id -> cached note/groups
        self._notes_map: dict | None = None   # targetUserId->note (once/session)
        self._scr_fetch_q: queue.Queue = queue.Queue()
        self._scr_queued: set[str] = set()    # user_ids awaiting a lookup
        self._scr_ok_queued: set[str] = set()  # user_ids awaiting auto-OK
        self._closing = False
        threading.Thread(target=self._scr_fetch_worker, daemon=True).start()

        self._style()

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_listener = tk.Frame(self.nb, bg=BG)
        self.tab_instance = tk.Frame(self.nb, bg=BG)
        self.tab_screening = tk.Frame(self.nb, bg=BG)
        self.tab_incidents = tk.Frame(self.nb, bg=BG)
        self.tab_settings = tk.Frame(self.nb, bg=BG)
        self.nb.add(self.tab_listener, text="  Listener  ")
        self.nb.add(self.tab_instance, text="  Instance  ")
        self.nb.add(self.tab_screening, text="  Screening  ")
        self.nb.add(self.tab_incidents, text="  Incidents  ")
        self.nb.add(self.tab_settings, text="  Settings  ")

        self._build_listener(args)
        self._build_instance()
        self._build_screening()
        self._build_incidents()
        self._build_settings()

        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_tick = 0
        self._poll()

        if args.autostart:
            root.after(200, self.toggle)
        if args.screenshot:
            root.after(args.shot_delay, lambda: self._screenshot(args.screenshot))

    # ================= styling =================
    def _style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG,
                        fieldbackground=PANEL, font=FONT)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=DIM,
                        padding=(14, 6))
        style.map("TNotebook.Tab",
                  background=[("selected", "#37373d")],
                  foreground=[("selected", FG)])
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL,
                        foreground=FG, arrowcolor=FG)
        style.map("TCombobox",
                  fieldbackground=[("readonly", PANEL)],
                  foreground=[("readonly", FG)],
                  selectbackground=[("readonly", PANEL)],
                  selectforeground=[("readonly", FG)])
        style.configure("TSpinbox", fieldbackground=PANEL, background=PANEL,
                        foreground=FG, arrowcolor=FG)
        style.configure("Start.TButton", background="#0e639c",
                        foreground="white", padding=(14, 6))
        style.map("Start.TButton", background=[("active", "#1177bb")])
        style.configure("Stop.TButton", background="#a1260d",
                        foreground="white", padding=(14, 6))
        style.map("Stop.TButton", background=[("active", "#c42b1c")])
        style.configure("Tool.TButton", background=PANEL, foreground=FG,
                        padding=(10, 5))
        style.map("Tool.TButton", background=[("active", "#37373d")])
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=FG, rowheight=26, borderwidth=0)
        style.configure("Treeview.Heading", background="#333333",
                        foreground=FG, relief="flat")
        style.map("Treeview", background=[("selected", "#094771")])

    # ================= Listener tab =================
    def _build_listener(self, args: argparse.Namespace) -> None:
        root = self.tab_listener
        bar = tk.Frame(root, bg=BG)
        bar.pack(fill="x", padx=10, pady=(10, 4))

        self.start_btn = ttk.Button(bar, text="▶  Start", style="Start.TButton",
                                    command=self.toggle)
        self.start_btn.pack(side="left")

        tk.Label(bar, text="Device", bg=BG, fg=DIM, font=FONT).pack(
            side="left", padx=(16, 4))
        devices = ([VRCHAT_DISCORD_PRESET, "(default speakers)"]
                   + autoclip.loopback_device_names())
        self.device_cb = ttk.Combobox(bar, values=devices, width=30,
                                      state="readonly")
        wanted = args.device or self.cfg["device"]
        match = next((d for d in devices if wanted.lower() in d.lower()),
                     devices[0]) if wanted else devices[0]
        self.device_cb.set(match)
        self.device_cb.pack(side="left")

        tk.Label(bar, text="Model", bg=BG, fg=DIM, font=FONT).pack(
            side="left", padx=(16, 4))
        self.model_cb = ttk.Combobox(bar, values=list(autoclip.MODELS),
                                     width=7, state="readonly")
        default_model = args.model or self.cfg["model"] or \
            ("large" if autoclip.model_downloaded("large") else "small")
        self.model_cb.set(default_model)
        self.model_cb.pack(side="left")

        tk.Label(bar, text="Hotkey", bg=BG, fg=DIM, font=FONT).pack(
            side="left", padx=(16, 4))
        self.hotkey_var = tk.StringVar(value=self.cfg["hotkey"])
        self.hotkey_entry = tk.Entry(bar, textvariable=self.hotkey_var, width=6,
                                     bg=PANEL, fg=FG, insertbackground=FG,
                                     relief="flat", font=FONT)
        self.hotkey_entry.pack(side="left", ipady=3)

        tk.Label(bar, text="Cooldown", bg=BG, fg=DIM, font=FONT).pack(
            side="left", padx=(16, 4))
        self.cooldown_var = tk.StringVar(value=str(self.cfg["cooldown"]))
        self.cooldown_sp = ttk.Spinbox(bar, from_=1, to=120, width=5,
                                       textvariable=self.cooldown_var)
        self.cooldown_sp.pack(side="left")
        tk.Label(bar, text="s", bg=BG, fg=DIM, font=FONT).pack(side="left")

        ttk.Button(bar, text="Test", style="Tool.TButton",
                   command=self.test_clip).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Triggers", style="Tool.TButton",
                   command=self.edit_triggers).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Clear", style="Tool.TButton",
                   command=self.clear_log).pack(side="right")

        # ---------------- status / level ----------------
        status_row = tk.Frame(root, bg=BG)
        status_row.pack(fill="x", padx=10, pady=(4, 2))
        self.status_var = tk.StringVar(value="Stopped.")
        self.status_lbl = tk.Label(status_row, textvariable=self.status_var,
                                   bg=BG, fg=DIM, font=FONT, anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)
        tk.Label(status_row, text="Level", bg=BG, fg=DIM, font=FONT).pack(
            side="left", padx=(8, 6))
        self.meter = tk.Canvas(status_row, width=160, height=12, bg=PANEL,
                               highlightthickness=0)
        self.meter.pack(side="left")
        self.meter_bar = self.meter.create_rectangle(0, 0, 0, 12, fill=GREEN,
                                                     width=0)
        self.level_smooth = 0.0

        # ---------------- stats ----------------
        stats = tk.Frame(root, bg=PANEL)
        stats.pack(fill="x", padx=10, pady=(4, 6))
        self.stat_vars = {}
        for key, label in [("state", "Status"), ("uptime", "Uptime"),
                           ("heard", "Lines heard"), ("triggers", "Clips fired"),
                           ("last", "Last trigger")]:
            cell = tk.Frame(stats, bg=PANEL)
            cell.pack(side="left", padx=14, pady=6)
            tk.Label(cell, text=label, bg=PANEL, fg=DIM,
                     font=("Segoe UI", 8)).pack(anchor="w")
            var = tk.StringVar(value="—")
            tk.Label(cell, textvariable=var, bg=PANEL, fg=FG,
                     font=("Segoe UI", 11, "bold")).pack(anchor="w")
            self.stat_vars[key] = var
        self.stat_vars["state"].set("stopped")

        # ---------------- log ----------------
        self.log_box = ScrolledText(root, bg="#161616", fg=FG, font=MONO,
                                    relief="flat", wrap="word", state="disabled")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_box.tag_configure("dim", foreground=DIM)
        self.log_box.tag_configure("status", foreground=YELLOW)
        self.log_box.tag_configure("trigger", foreground=RED,
                                   font=("Consolas", 10, "bold"))

    # ================= Instance tab =================
    def _build_instance(self) -> None:
        root = self.tab_instance
        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=12, pady=(12, 4))
        self.world_var = tk.StringVar(value="Not in a world (is VRChat running?)")
        tk.Label(head, textvariable=self.world_var, bg=BG, fg=FG,
                 font=("Segoe UI", 13, "bold"), anchor="w").pack(fill="x")
        self.world_detail_var = tk.StringVar(value="")
        tk.Label(head, textvariable=self.world_detail_var, bg=BG, fg=DIM,
                 font=MONO, anchor="w").pack(fill="x")
        self.player_count_var = tk.StringVar(value="0 players")
        tk.Label(head, textvariable=self.player_count_var, bg=BG, fg=GREEN,
                 font=FONT, anchor="w").pack(fill="x", pady=(2, 0))

        cols = ("name", "user_id", "joined")
        self.player_tree = ttk.Treeview(root, columns=cols, show="headings")
        self.player_tree.heading("name", text="Display name")
        self.player_tree.heading("user_id", text="User ID")
        self.player_tree.heading("joined", text="Joined at")
        self.player_tree.column("name", width=280)
        self.player_tree.column("user_id", width=340)
        self.player_tree.column("joined", width=110, anchor="center")
        self.player_tree.pack(fill="both", expand=True, padx=12, pady=(6, 6))

        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(foot, text="Copy roster", style="Tool.TButton",
                   command=self.copy_roster).pack(side="left")
        tk.Label(foot, text="Roster is read from VRChat's local output log "
                            "and updates automatically.",
                 bg=BG, fg=DIM, font=FONT).pack(side="left", padx=10)

    def _refresh_instance(self, snap: dict) -> None:
        if snap["world_name"]:
            self.world_var.set(snap["world_name"])
        else:
            self.world_var.set("Not in a world (is VRChat running?)")
        detail = ""
        if snap["world_id"]:
            detail = f"{snap['world_id']}:{snap['instance_id']}"
        self.world_detail_var.set(detail)
        players = sorted(snap["players"], key=lambda p: p["joined_at"])
        self.player_count_var.set(f"{len(players)} players")
        self.player_tree.delete(*self.player_tree.get_children())
        for p in players:
            self.player_tree.insert("", "end", values=(
                p["name"], p["user_id"] or "—",
                time.strftime("%H:%M:%S", time.localtime(p["joined_at"]))))

    def copy_roster(self) -> None:
        snap = self.watcher.snapshot()
        lines = [f"{snap['world_name']}  {snap['world_id']}:{snap['instance_id']}"]
        for p in sorted(snap["players"], key=lambda p: p["name"].lower()):
            uid = f"  [{p['user_id']}]" if p["user_id"] else ""
            lines.append(f"- {p['name']}{uid}")
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.append_log("roster copied to clipboard", "status")

    # ================= Screening tab =================
    def _build_screening(self) -> None:
        root = self.tab_screening
        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(head, text="Instance screening", bg=BG, fg=FG,
                 font=("Segoe UI", 13, "bold"), anchor="w").pack(fill="x")
        tk.Label(head, text="Everyone currently in the instance. Each new "
                            "player's note and groups are looked up once and "
                            "cached locally; the list updates as people "
                            "join/leave (needs a VRChat login on Settings). "
                            "Members of the watched group are auto-verified.",
                 bg=BG, fg=DIM, font=FONT, anchor="w", justify="left",
                 wraplength=980).pack(fill="x")
        ctl = tk.Frame(head, bg=BG)
        ctl.pack(fill="x", pady=(6, 0))
        tk.Label(ctl, text="Group filter", bg=BG, fg=DIM, font=FONT).pack(
            side="left")
        self.group_filter_var = tk.StringVar(
            value=self.cfg.get("group_filter", ""))
        grp_entry = tk.Entry(ctl, textvariable=self.group_filter_var, width=30,
                             bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                             font=FONT)
        grp_entry.pack(side="left", padx=6, ipady=3)
        grp_entry.bind("<Return>", lambda e: self.scr_refresh())
        tk.Label(ctl, text="group name or grp_ ID; matching members are "
                           "highlighted. Enter to apply.",
                 bg=BG, fg=DIM, font=FONT).pack(side="left", padx=6)

        cnt = tk.Frame(head, bg=BG)
        cnt.pack(fill="x", pady=(6, 0))
        self.scr_verified_var = tk.StringVar(value="✔ Verified: 0")
        tk.Label(cnt, textvariable=self.scr_verified_var, bg=BG, fg=GREEN,
                 font=("Segoe UI", 12, "bold")).pack(side="left")
        self.scr_unverified_var = tk.StringVar(value="✖ Unverified: 0")
        tk.Label(cnt, textvariable=self.scr_unverified_var, bg=BG, fg=RED,
                 font=("Segoe UI", 12, "bold")).pack(side="left", padx=(20, 0))

        self.scr_info_var = tk.StringVar(value="")
        tk.Label(head, textvariable=self.scr_info_var, bg=BG, fg=YELLOW,
                 font=FONT, anchor="w").pack(fill="x", pady=(2, 0))

        cols = ("name", "user_id", "tagged", "groups")
        self.scr_tree = ttk.Treeview(root, columns=cols, show="headings",
                                     selectmode="browse")
        for cid, text, w, anchor in [
                ("name", "Display name", 240, "w"),
                ("user_id", "User ID", 300, "w"),
                ("tagged", "Tag", 90, "center"),
                ("groups", "Matched groups", 220, "w")]:
            self.scr_tree.heading(cid, text=text)
            self.scr_tree.column(cid, width=w, anchor=anchor)
        self.scr_tree.tag_configure("tagged", foreground=GREEN)
        self.scr_tree.tag_configure("matched", foreground=YELLOW)
        self.scr_tree.pack(fill="both", expand=True, padx=12, pady=(6, 6))

        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(foot, text="Refresh", style="Tool.TButton",
                   command=self.scr_refresh).pack(side="left")
        ttk.Button(foot, text="Over", style="Tool.TButton",
                   command=lambda: self.scr_age_action("over")).pack(
            side="left", padx=(12, 6))
        ttk.Button(foot, text="Under", style="Tool.TButton",
                   command=lambda: self.scr_age_action("under")).pack(
            side="left", padx=6)
        ttk.Button(foot, text="In Range", style="Tool.TButton",
                   command=self.scr_in_range).pack(side="left", padx=6)
        tk.Label(foot, text="Over / Under log an age incident; In Range tags "
                            "the player's note with the filter word.",
                 bg=BG, fg=DIM, font=FONT).pack(side="left", padx=10)

    def _on_tab_changed(self, _event=None) -> None:
        if self.nb.select() == str(self.tab_screening):
            self._scr_sync()

    def _selected_scr_row(self) -> dict | None:
        sel = self.scr_tree.selection()
        return self.scr_rows.get(sel[0]) if sel else None

    def _note_has_filter(self, note: str) -> bool:
        word = self.cfg.get("note_filter", "").strip().lower()
        return bool(word and note and word in note.lower())

    def _matched_groups(self, groups: list, group_filter: str) -> list:
        if not group_filter:
            return []
        out = []
        for g in groups:
            hay = (f"{g.get('name', '')}\n{g.get('id', '')}\n"
                   f"{g.get('code', '')}").lower()
            if group_filter in hay:
                out.append(g.get("name") or g.get("id"))
        return out

    def scr_refresh(self) -> None:
        """Manual refresh: rebuild the view (re-evaluating the group filter and
        queuing any unseen players), then re-pull notes for the current roster
        in one bulk call so externally-changed notes show up."""
        self.cfg["group_filter"] = self.group_filter_var.get().strip()
        self._scr_sync()
        if self.vrc_api and self.vrc_api.user:
            self.scr_info_var.set("Refreshing notes…")
            threading.Thread(target=self._scr_reload_notes,
                             daemon=True).start()

    def _scr_reload_notes(self) -> None:
        """One bulk note fetch; reconcile cached notes for on-screen users."""
        self._notes_map = None
        self._ensure_notes_map()
        m = self._notes_map or {}
        for row in list(self.scr_rows.values()):
            uid = row.get("user_id")
            if uid and uid in self.scr_db:
                new = m.get(uid, "")
                if new != self.scr_db[uid].get("note", ""):
                    self.scr_db[uid]["note"] = new
                    self.db.upsert_user(uid, self.scr_db[uid])
                    self.q.put(("scr_update", uid))
        self.q.put(("scr_synced", None))

    def _scr_sync(self) -> None:
        """Rebuild the list from the current roster. Known users are drawn
        straight from the local DB (no network); only users we've never seen
        are queued for a one-time note/group lookup."""
        snap = self.watcher.snapshot()
        players = sorted(snap["players"], key=lambda p: p["name"].lower())
        group_filter = self.group_filter_var.get().strip().lower()
        logged_in = bool(self.vrc_api and self.vrc_api.user)
        prev_sel = self.scr_tree.selection()
        self.scr_rows = {}
        self.scr_tree.delete(*self.scr_tree.get_children())
        for p in players:
            uid = p.get("user_id") or ""
            iid = uid or f"name:{p['name']}"
            rec = self.scr_db.get(uid) if uid else None
            note = rec.get("note", "") if rec else ""
            matched = self._matched_groups(rec.get("groups", []),
                                           group_filter) if rec else []
            tagged = self._note_has_filter(note)
            self.scr_rows[iid] = {
                "name": p["name"], "user_id": uid,
                "joined_at": p.get("joined_at", time.time()),
                "note": note, "groups": matched, "known": rec is not None}
            if uid and rec is None and logged_in and uid not in self._scr_queued:
                self._scr_queued.add(uid)
                self._scr_fetch_q.put(("fetch", uid, p["name"]))
            # cached, in the watched group, but not yet verified → auto-OK
            elif (uid and matched and not tagged and logged_in
                  and self.cfg.get("note_filter", "").strip()
                  and uid not in self._scr_ok_queued):
                self._scr_ok_queued.add(uid)
                self._scr_fetch_q.put(("ok", uid, p["name"]))
            tags = ("tagged",) if tagged else (
                ("matched",) if matched else ())
            tag_txt = ("✔" if tagged
                       else ("?" if (uid and rec is None and logged_in)
                             else "—"))
            self.scr_tree.insert("", "end", iid=iid, values=(
                p["name"], uid or "—", tag_txt, ", ".join(matched)), tags=tags)
        if prev_sel and prev_sel[0] in self.scr_rows:
            self.scr_tree.selection_set(prev_sel[0])
        self._scr_status(len(players), logged_in)

    def _scr_update_counts(self) -> None:
        verified = sum(1 for r in self.scr_rows.values()
                       if self._note_has_filter(r["note"]))
        self.scr_verified_var.set(f"✔ Verified: {verified}")
        self.scr_unverified_var.set(
            f"✖ Unverified: {len(self.scr_rows) - verified}")

    def _scr_status(self, n_players: int, logged_in: bool) -> None:
        self._scr_update_counts()
        if n_players == 0:
            self.scr_info_var.set("No players in the instance right now "
                                  "(is VRChat running and in a world?).")
        elif not logged_in:
            self.scr_info_var.set("Listed from the log. Log in on Settings to "
                                  "load notes and groups.")
        else:
            n_tag = sum(1 for r in self.scr_rows.values()
                        if self._note_has_filter(r["note"]))
            msg = f"{n_players} in instance · {n_tag} tagged"
            pend = len(self._scr_queued)
            if pend:
                msg += f" · checking {pend} new player(s)…"
            self.scr_info_var.set(msg)

    def _scr_fetch_worker(self) -> None:
        """One gentle background worker. 'fetch' looks up an unseen user's note
        and groups exactly once and caches them; 'ok' auto-verifies a cached
        group member. It paces itself to stay under VRChat's rate limit; the DB
        means we never re-check someone we already know."""
        while not self._closing:
            try:
                kind, uid, name = self._scr_fetch_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if not (self.vrc_api and self.vrc_api.user):
                self._scr_queued.discard(uid)
                self._scr_ok_queued.discard(uid)
                continue
            if kind == "ok":
                self._scr_ok_queued.discard(uid)
                self._perform_autook(
                    uid, name, self.scr_db.get(uid, {}).get("note", ""))
                time.sleep(1.5)
                continue
            # kind == "fetch"
            self._ensure_notes_map()
            note = (self._notes_map or {}).get(uid, "")
            groups = []
            try:
                for g in self.vrc_api.get_user_groups(uid):
                    groups.append({"name": g.get("name", "") or "",
                                   "id": g.get("groupId", "") or "",
                                   "code": g.get("shortCode", "") or ""})
            except Exception as e:
                if "429" in str(e):
                    time.sleep(30)           # rate limited — back off, retry
                    self._scr_fetch_q.put(("fetch", uid, name))
                    continue
                # user hides groups or other error: cache with none
            rec = {"name": name, "note": note, "groups": groups,
                   "checked_at": time.time()}
            self.scr_db[uid] = rec
            self.db.upsert_user(uid, rec)
            self._scr_queued.discard(uid)
            self.q.put(("scr_update", uid))
            # auto-OK: in the watched group and not already verified
            gf = self.group_filter_var.get().strip().lower()
            if self._matched_groups(groups, gf) and \
                    not self._note_has_filter(note):
                self._perform_autook(uid, name, note)
            time.sleep(1.5)                  # pace between users

    def _perform_autook(self, uid: str, name: str, existing: str) -> None:
        """Add the filter word to a group member's note (verify them) unless
        it is already there. Posts an 'scr_tagged' event on success."""
        word = self.cfg.get("note_filter", "").strip()
        if not word or word.lower() in (existing or "").lower():
            return
        new_note = f"{existing} {word}".strip() if existing else word
        try:
            self.vrc_api.update_user_note(uid, new_note)
        except Exception as e:
            if "429" in str(e):
                time.sleep(30)
                self._scr_ok_queued.add(uid)
                self._scr_fetch_q.put(("ok", uid, name))
            return
        self.q.put(("scr_tagged", {"iid": uid, "uid": uid, "note": new_note,
                                   "name": name, "word": word, "auto": True}))

    def _ensure_notes_map(self) -> None:
        """Fetch every note you've written, once per session, into a
        targetUserId->note map. Cheap (one call) and reused for all users."""
        if self._notes_map is not None:
            return
        for attempt in range(3):
            try:
                m = {}
                for n in self.vrc_api.list_user_notes():
                    tid = n.get("targetUserId")
                    if tid:
                        m[tid] = n.get("note", "") or ""
                self._notes_map = m
                return
            except Exception as e:
                if "429" in str(e) and attempt < 2:
                    time.sleep(15)
                    continue
                self.q.put(("scr_error", f"couldn't load notes: {e}"))
                self._notes_map = {}   # give up for now; Refresh retries
                return

    def scr_age_action(self, kind: str) -> None:
        row = self._selected_scr_row()
        if not row:
            self.scr_info_var.set("Select a player first.")
            return
        age = simpledialog.askinteger(
            f"{kind.capitalize()} range",
            f"Enter {row['name']}'s age:",
            parent=self.root, minvalue=1, maxvalue=120)
        if age is None:
            return
        snap = self.watcher.snapshot()
        player = {"name": row["name"], "user_id": row["user_id"],
                  "joined_at": row.get("joined_at", time.time())}
        inc = self.store.add(
            trigger=f"age {kind} range ({age})",
            transcript=[f"Manual screening: {row['name']} marked "
                        f"{kind.upper()} range — reported age {age}."],
            world_name=snap["world_name"], world_id=snap["world_id"],
            instance_id=snap["instance_id"], players=[player])
        self._reload_incidents()
        self.append_log(f"screening: incident for {row['name']} "
                        f"({kind} range, age {age})", "trigger")
        self.scr_info_var.set(f"Logged incident {inc['id']} — {row['name']} "
                              f"{kind} range (age {age}).")
        threading.Thread(target=self._take_shot, args=(inc["id"],),
                         daemon=True).start()

    def scr_in_range(self) -> None:
        row = self._selected_scr_row()
        if not row:
            self.scr_info_var.set("Select a player first.")
            return
        if not row["user_id"]:
            self.scr_info_var.set("That player has no user ID in the log yet.")
            return
        if self.vrc_api is None or self.vrc_api.user is None:
            self.scr_info_var.set("Log in on the Settings tab first.")
            return
        word = self.cfg.get("note_filter", "").strip()
        if not word:
            self.scr_info_var.set("Set a filter word on the Settings tab first.")
            return
        existing = row.get("note", "")
        if word.lower() in existing.lower():
            self.scr_info_var.set(f"{row['name']} is already tagged "
                                  f"'{word}'.")
            return
        new_note = f"{existing} {word}".strip() if existing else word
        uid = row["user_id"]
        self.scr_info_var.set(f"Tagging {row['name']}…")
        selected_iid = self.scr_tree.selection()

        def work():
            try:
                self.vrc_api.update_user_note(uid, new_note)
                self.q.put(("scr_tagged",
                            {"iid": selected_iid[0] if selected_iid else uid,
                             "uid": uid, "note": new_note, "name": row["name"],
                             "word": word}))
            except Exception as e:
                self.q.put(("scr_error", f"couldn't tag note: {e}"))
        threading.Thread(target=work, daemon=True).start()

    # ================= Incidents tab =================
    def _build_incidents(self) -> None:
        root = self.tab_incidents
        pane = tk.PanedWindow(root, orient="horizontal", bg=BG, sashwidth=6,
                              relief="flat")
        pane.pack(fill="both", expand=True, padx=12, pady=12)

        left = tk.Frame(pane, bg=BG)
        cols = ("time", "trigger", "world", "players", "clip", "status")
        self.inc_tree = ttk.Treeview(left, columns=cols, show="headings",
                                     selectmode="browse")
        for cid, text, w, anchor in [
                ("time", "Time", 130, "w"), ("trigger", "Trigger", 120, "w"),
                ("world", "World", 170, "w"), ("players", "👥", 40, "center"),
                ("clip", "Clip", 50, "center"), ("status", "Status", 80, "center")]:
            self.inc_tree.heading(cid, text=text)
            self.inc_tree.column(cid, width=w, anchor=anchor)
        self.inc_tree.pack(fill="both", expand=True)
        self.inc_tree.bind("<<TreeviewSelect>>", lambda e: self._show_incident())
        pane.add(left, minsize=420)

        right = tk.Frame(pane, bg=BG)
        self.inc_detail = ScrolledText(right, bg="#161616", fg=FG, font=MONO,
                                       relief="flat", wrap="word", height=18)
        self.inc_detail.pack(fill="both", expand=True)

        tk.Label(right, text="Notes", bg=BG, fg=DIM, font=FONT).pack(
            anchor="w", pady=(6, 0))
        self.inc_notes = tk.Text(right, bg=PANEL, fg=FG, insertbackground=FG,
                                 relief="flat", font=FONT, height=3)
        self.inc_notes.pack(fill="x", pady=(2, 6))

        rows = [tk.Frame(right, bg=BG), tk.Frame(right, bg=BG)]
        for r in rows:
            r.pack(fill="x", pady=(0, 4))
        for row, text, cmd in [(0, "Copy report", self.copy_report),
                               (0, "Save notes", self.save_notes),
                               (0, "Open clip", self.open_clip),
                               (1, "Open screenshot", self.open_shot),
                               (1, "Mark reported", self.mark_reported),
                               (1, "Delete", self.delete_incident)]:
            ttk.Button(rows[row], text=text, style="Tool.TButton",
                       command=cmd).pack(side="left", padx=(0, 6))
        pane.add(right, minsize=380)

        self._reload_incidents()

    def _selected_incident(self) -> dict | None:
        sel = self.inc_tree.selection()
        return self.store.get(sel[0]) if sel else None

    def _reload_incidents(self) -> None:
        self.inc_tree.delete(*self.inc_tree.get_children())
        for inc in reversed(self.store.incidents):
            self.inc_tree.insert("", "end", iid=inc["id"], values=(
                time.strftime("%m-%d %H:%M:%S",
                              time.localtime(inc["created_at"])),
                inc["trigger"], inc["world_name"] or "?",
                len(inc["players"]),
                "✔" if inc["clip_path"] else "—",
                inc["status"]))

    def _show_incident(self) -> None:
        inc = self._selected_incident()
        if not inc:
            return
        self.inc_detail.configure(state="normal")
        self.inc_detail.delete("1.0", "end")
        self.inc_detail.insert("end", report.build_report(inc))
        self.inc_detail.configure(state="disabled")
        self.inc_notes.delete("1.0", "end")
        self.inc_notes.insert("1.0", inc.get("notes", ""))

    def copy_report(self) -> None:
        inc = self._selected_incident()
        if inc:
            self.root.clipboard_clear()
            self.root.clipboard_append(report.build_report(inc))
            self.append_log(f"report for incident {inc['id']} copied", "status")

    def save_notes(self) -> None:
        inc = self._selected_incident()
        if inc:
            self.store.update(inc["id"],
                              notes=self.inc_notes.get("1.0", "end").strip())
            self._show_incident()

    def open_clip(self) -> None:
        inc = self._selected_incident()
        if inc and inc["clip_path"] and Path(inc["clip_path"]).exists():
            os.startfile(inc["clip_path"])
        elif inc:
            self.append_log("no clip linked to this incident (yet)", "status")

    def open_shot(self) -> None:
        inc = self._selected_incident()
        if inc and inc.get("screenshot_path") and \
                Path(inc["screenshot_path"]).exists():
            os.startfile(inc["screenshot_path"])
        elif inc:
            self.append_log("no screenshot for this incident", "status")

    def mark_reported(self) -> None:
        inc = self._selected_incident()
        if inc:
            self.store.update(inc["id"], status="reported")
            self._reload_incidents()

    def delete_incident(self) -> None:
        inc = self._selected_incident()
        if inc:
            self.store.delete(inc["id"])
            self._reload_incidents()
            self.inc_detail.configure(state="normal")
            self.inc_detail.delete("1.0", "end")
            self.inc_detail.configure(state="disabled")

    # ================= Settings tab =================
    def _build_settings(self) -> None:
        root = self.tab_settings
        pad = dict(padx=16, pady=4)

        def section(title):
            f = tk.Frame(root, bg=BG)
            f.pack(fill="x", anchor="w", pady=(12, 0))
            tk.Label(f, text=title, bg=BG, fg=BLUE,
                     font=("Segoe UI", 11, "bold")).pack(anchor="w", **pad)
            return f

        s1 = section("In-VR notifications")
        self.xso_var = tk.BooleanVar(value=self.cfg["xsoverlay"])
        ttk.Checkbutton(s1, text="XSOverlay / OVR Toolkit popup on trigger "
                                 "(private — only you see it)",
                        variable=self.xso_var).pack(anchor="w", **pad)
        self.chatbox_var = tk.BooleanVar(value=self.cfg["chatbox"])
        ttk.Checkbutton(s1, text="VRChat chatbox message on trigger "
                                 "(PUBLIC — players near you can read it)",
                        variable=self.chatbox_var).pack(anchor="w", **pad)
        ttk.Button(s1, text="Send test notification", style="Tool.TButton",
                   command=self.test_notification).pack(anchor="w", **pad)

        s2 = section("Medal clips folder")
        row = tk.Frame(s2, bg=BG)
        row.pack(fill="x", anchor="w", **pad)
        self.medal_var = tk.StringVar(value=self.cfg["medal_dir"])
        tk.Entry(row, textvariable=self.medal_var, width=70, bg=PANEL, fg=FG,
                 insertbackground=FG, relief="flat", font=FONT).pack(
            side="left", ipady=3)
        ttk.Button(row, text="Browse…", style="Tool.TButton",
                   command=self.browse_medal).pack(side="left", padx=6)

        s3 = section("VRChat account (optional)")
        tk.Label(s3, text="Used to look up user IDs and profiles for reports. "
                          "Login goes directly to VRChat's API; only the "
                          "session cookie is stored locally. VRChat requires "
                          "real contact info (your email or Discord) in every "
                          "API request — enter it below or login is refused.",
                 bg=BG, fg=DIM, font=FONT, wraplength=700,
                 justify="left").pack(anchor="w", **pad)
        crow = tk.Frame(s3, bg=BG)
        crow.pack(anchor="w", **pad)
        tk.Label(crow, text="Contact", bg=BG, fg=DIM, font=FONT).pack(
            side="left")
        self.vrc_contact_var = tk.StringVar(value=self.cfg.get("vrc_contact", ""))
        tk.Entry(crow, textvariable=self.vrc_contact_var, width=40, bg=PANEL,
                 fg=FG, insertbackground=FG, relief="flat", font=FONT).pack(
            side="left", padx=6, ipady=3)
        tk.Label(crow, text="e.g. you@email.com", bg=BG, fg=DIM,
                 font=FONT).pack(side="left")
        form = tk.Frame(s3, bg=BG)
        form.pack(anchor="w", **pad)
        tk.Label(form, text="Username", bg=BG, fg=DIM, font=FONT).grid(
            row=0, column=0, sticky="w")
        self.vrc_user_var = tk.StringVar()
        tk.Entry(form, textvariable=self.vrc_user_var, width=28, bg=PANEL,
                 fg=FG, insertbackground=FG, relief="flat",
                 font=FONT).grid(row=0, column=1, padx=6, ipady=3)
        tk.Label(form, text="Password", bg=BG, fg=DIM, font=FONT).grid(
            row=0, column=2, sticky="w")
        self.vrc_pass_var = tk.StringVar()
        tk.Entry(form, textvariable=self.vrc_pass_var, width=22, show="•",
                 bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                 font=FONT).grid(row=0, column=3, padx=6, ipady=3)
        self.vrc_login_btn = ttk.Button(form, text="Log in",
                                        style="Tool.TButton",
                                        command=self.vrc_login)
        self.vrc_login_btn.grid(row=0, column=4, padx=6)

        self.twofa_row = tk.Frame(s3, bg=BG)
        tk.Label(self.twofa_row, text="2FA code", bg=BG, fg=DIM,
                 font=FONT).pack(side="left")
        self.vrc_2fa_var = tk.StringVar()
        tk.Entry(self.twofa_row, textvariable=self.vrc_2fa_var, width=10,
                 bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                 font=FONT).pack(side="left", padx=6, ipady=3)
        ttk.Button(self.twofa_row, text="Verify", style="Tool.TButton",
                   command=self.vrc_verify_2fa).pack(side="left")

        self.vrc_status_var = tk.StringVar(value="Not logged in.")
        tk.Label(s3, textvariable=self.vrc_status_var, bg=BG, fg=DIM,
                 font=FONT).pack(anchor="w", **pad)

        s_scr = section("Screening")
        tk.Label(s_scr, text="Filter word — written to a player's private note "
                             "when you mark them In Range, and matched to show "
                             "who is already tagged. Group filter — a group-name "
                             "substring; instance players in a matching group "
                             "are highlighted.",
                 bg=BG, fg=DIM, font=FONT, wraplength=700,
                 justify="left").pack(anchor="w", **pad)
        scr_form = tk.Frame(s_scr, bg=BG)
        scr_form.pack(anchor="w", **pad)
        tk.Label(scr_form, text="Filter word", bg=BG, fg=DIM, font=FONT).grid(
            row=0, column=0, sticky="w")
        self.note_filter_var = tk.StringVar(value=self.cfg.get("note_filter", ""))
        tk.Entry(scr_form, textvariable=self.note_filter_var, width=24, bg=PANEL,
                 fg=FG, insertbackground=FG, relief="flat",
                 font=FONT).grid(row=0, column=1, padx=6, ipady=3)
        tk.Label(scr_form, text="Group filter", bg=BG, fg=DIM, font=FONT).grid(
            row=0, column=2, sticky="w", padx=(12, 0))
        # shares the same variable as the field on the Screening tab
        tk.Entry(scr_form, textvariable=self.group_filter_var, width=24,
                 bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                 font=FONT).grid(row=0, column=3, padx=6, ipady=3)

        s4 = section("")
        ttk.Button(s4, text="Save settings", style="Start.TButton",
                   command=self.save_settings).pack(anchor="w", **pad)

        # check for an existing valid session in the background
        threading.Thread(target=self._vrc_check_session, daemon=True).start()

    def browse_medal(self) -> None:
        d = filedialog.askdirectory(initialdir=self.medal_var.get())
        if d:
            self.medal_var.set(d)

    def test_notification(self) -> None:
        if self.xso_var.get():
            notify.xsoverlay_notify("Mod Suite test",
                                    "Notifications are working.")
        if self.chatbox_var.get():
            notify.chatbox_message("mod suite: test notification")
        self.append_log("test notification sent", "status")

    def save_settings(self) -> None:
        self.cfg.update(
            device=self.device_cb.get(), model=self.model_cb.get(),
            hotkey=self.hotkey_var.get().strip(),
            cooldown=float(self.cooldown_var.get() or
                           autoclip.COOLDOWN_SECONDS),
            xsoverlay=self.xso_var.get(), chatbox=self.chatbox_var.get(),
            medal_dir=self.medal_var.get().strip(),
            note_filter=self.note_filter_var.get().strip(),
            group_filter=self.group_filter_var.get().strip(),
            vrc_contact=self.vrc_contact_var.get().strip())
        try:
            CONFIG_PATH.write_text(json.dumps(self.cfg, indent=2),
                                   encoding="utf-8")
            self.append_log("settings saved", "status")
        except OSError as e:
            self.append_log(f"couldn't save settings: {e}", "trigger")

    # ---------- VRChat API (all network calls off the GUI thread) ----------
    def _get_api(self):
        if self.vrc_api is None:
            import vrc_api as mod
            self.vrc_api = mod.VRChatAPI(
                contact=self.cfg.get("vrc_contact", ""))
        return self.vrc_api

    def _current_contact(self) -> str:
        """Contact string from the Settings field; also refresh the API's
        User-Agent so a just-edited value takes effect without a restart."""
        c = (self.vrc_contact_var.get().strip()
             if hasattr(self, "vrc_contact_var")
             else self.cfg.get("vrc_contact", ""))
        self.cfg["vrc_contact"] = c
        if self.vrc_api is not None:
            import vrc_api as mod
            self.vrc_api.s.headers["User-Agent"] = mod.build_user_agent(c)
        return c

    def _vrc_check_session(self) -> None:
        import vrc_api as mod
        if not mod.is_valid_contact(self._current_contact()):
            return   # VRChat would 403 without a real contact
        try:
            user = self._get_api().check_session()
            if user:
                self.q.put(("vrc_login",
                            f"Logged in as {user.get('displayName', '?')} "
                            "(saved session)"))
        except Exception:
            pass

    def vrc_login(self) -> None:
        import vrc_api as mod
        if not mod.is_valid_contact(self._current_contact()):
            self.vrc_status_var.set(
                "VRChat needs your real contact info first — fill in the "
                "'Contact' field above (your email or Discord).")
            return
        user = self.vrc_user_var.get().strip()
        pw = self.vrc_pass_var.get()
        if not user or not pw:
            self.vrc_status_var.set("Enter username and password first.")
            return
        self.vrc_status_var.set("Logging in…")

        def work():
            try:
                result = self._get_api().login(user, pw)
                if result == "ok":
                    name = self.vrc_api.user.get("displayName", "?")
                    self.q.put(("vrc_login", f"Logged in as {name}"))
                else:
                    self._2fa_method = result
                    self.q.put(("vrc_2fa", result))
            except Exception as e:
                self.q.put(("vrc_login_err", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def vrc_verify_2fa(self) -> None:
        code = self.vrc_2fa_var.get().strip()
        if not code:
            return
        self.vrc_status_var.set("Verifying 2FA…")

        def work():
            try:
                user = self._get_api().verify_2fa(code, self._2fa_method)
                self.q.put(("vrc_login",
                            f"Logged in as {user.get('displayName', '?')}"))
            except Exception as e:
                self.q.put(("vrc_login_err", str(e)))
        threading.Thread(target=work, daemon=True).start()

    # ================= actions =================
    def toggle(self) -> None:
        if self.running:
            self.stop_event.set()
            self.status_var.set("Stopping...")
            self.start_btn.state(["disabled"])
            return
        device = self.device_cb.get()
        if device == VRCHAT_DISCORD_PRESET:
            device_filter = VRCHAT_DISCORD_FILTERS
        elif device.startswith("(default"):
            device_filter = None
        else:
            device_filter = device
        try:
            cooldown = float(self.cooldown_var.get())
        except ValueError:
            cooldown = autoclip.COOLDOWN_SECONDS
        hotkey = self.hotkey_var.get().strip() or autoclip.MEDAL_HOTKEY

        self.stop_event = threading.Event()
        self.worker = threading.Thread(
            target=self._engine_worker,
            kwargs=dict(device_filter=device_filter,
                        model_size=self.model_cb.get(),
                        hotkey=hotkey, cooldown=cooldown),
            daemon=True)
        self.running = True
        self.started_at = time.time()
        self.heard_count = 0
        self.trigger_count = 0
        self.stat_vars["state"].set("starting")
        self.stat_vars["heard"].set("0")
        self.stat_vars["triggers"].set("0")
        self.stat_vars["last"].set("—")
        self.start_btn.configure(text="■  Stop", style="Stop.TButton")
        for w in (self.device_cb, self.model_cb, self.cooldown_sp):
            w.state(["disabled"])
        self.hotkey_entry.configure(state="disabled")
        self.worker.start()

    def _engine_worker(self, **kwargs) -> None:
        try:
            autoclip.run_engine(on_event=lambda k, p: self.q.put((k, p)),
                                stop_event=self.stop_event, **kwargs)
            self.q.put(("stopped", None))
        except Exception as e:
            self.q.put(("error", str(e)))

    def test_clip(self) -> None:
        hotkey = self.hotkey_var.get().strip() or autoclip.MEDAL_HOTKEY
        try:
            autoclip.press_hotkey(hotkey)
            self.append_log(f"pressed {hotkey.upper()} manually (test clip)",
                            "status")
        except RuntimeError as e:
            self.append_log(str(e), "trigger")

    def edit_triggers(self) -> None:
        path = autoclip.HERE / autoclip.TRIGGER_FILE_NAME
        if not path.exists():
            path.write_text("# one trigger word/phrase per line\n",
                            encoding="utf-8")
        subprocess.Popen(["notepad", str(path)])
        self.append_log("editing triggers.txt — restart listening to apply "
                        "changes", "status")

    def clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def on_close(self) -> None:
        self._closing = True
        self.stop_event.set()
        self.watcher.stop()
        try:
            self.db.close()
        except Exception:
            pass
        try:
            self.logfile.close()
        except OSError:
            pass
        self.root.after(150, self.root.destroy)

    # ================= incident creation =================
    def _create_incident(self, trigger_phrase: str) -> None:
        snap = self.watcher.snapshot()
        inc = self.store.add(
            trigger=trigger_phrase,
            transcript=list(self.heard_buffer),
            world_name=snap["world_name"], world_id=snap["world_id"],
            instance_id=snap["instance_id"], players=snap["players"])
        self.pending_incident = (inc["id"], time.time(), 3)
        self._reload_incidents()

        if self.xso_var.get():
            where = snap["world_name"] or "unknown world"
            notify.xsoverlay_notify(
                "Clip saved",
                f"'{trigger_phrase}' — {len(snap['players'])} players in "
                f"{where}")
        if self.chatbox_var.get():
            notify.chatbox_message("📎 clip saved")

        threading.Thread(target=self._link_clip, args=(inc["id"], time.time()),
                         daemon=True).start()
        threading.Thread(target=self._take_shot, args=(inc["id"],),
                         daemon=True).start()

    def _take_shot(self, inc_id: str) -> None:
        path = capture.grab_vrchat_window(inc_id)
        if path:
            self.q.put(("shot_taken", (inc_id, path)))

    def _link_clip(self, inc_id: str, fired_at: float) -> None:
        """Medal takes a few seconds to encode; retry a few times."""
        medal_dir = self.medal_var.get().strip() or self.cfg["medal_dir"]
        for delay in (5, 8, 12):
            time.sleep(delay)
            path = incidents.find_new_clip(medal_dir, fired_at)
            if path:
                self.q.put(("clip_found", (inc_id, path)))
                return
        self.q.put(("clip_missing", inc_id))

    # ================= event pump =================
    def append_log(self, text: str, tag: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        try:
            self.logfile.write(f"[{stamp}] {text}\n")
        except OSError:
            pass
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{stamp}] ", "dim")
        self.log_box.insert("end", text + "\n", tag)
        # keep the log from growing without bound
        if float(self.log_box.index("end-1c").split(".")[0]) > 2000:
            self.log_box.delete("1.0", "200.0")
        self.log_box.configure(state="disabled")
        self.log_box.see("end")

    def _handle(self, kind: str, payload) -> None:
        if kind == "level":
            self.level_smooth = max(payload, self.level_smooth * 0.85)
        elif kind == "heard":
            self.heard_count += 1
            self.stat_vars["heard"].set(str(self.heard_count))
            stamped = f"[{time.strftime('%H:%M:%S')}] {payload}"
            self.heard_buffer.append(stamped)
            self.append_log(payload, "")
            # extend a fresh incident's transcript with what came right after
            if self.pending_incident:
                inc_id, ts, left = self.pending_incident
                if left > 0 and time.time() - ts < 20:
                    inc = self.store.get(inc_id)
                    if inc:
                        inc["transcript"].append(stamped)
                        self.store.update(inc_id, transcript=inc["transcript"])
                    self.pending_incident = (inc_id, ts, left - 1)
                else:
                    self.pending_incident = None
        elif kind == "trigger":
            self.trigger_count += 1
            self.stat_vars["triggers"].set(str(self.trigger_count))
            self.stat_vars["last"].set(
                f"'{payload}' at {time.strftime('%H:%M:%S')}")
            self.append_log(f">>> TRIGGERED by '{payload}' — clip saved",
                            "trigger")
            self._create_incident(payload)
        elif kind == "clip_found":
            inc_id, path = payload
            self.store.update(inc_id, clip_path=path)
            self._reload_incidents()
            self.append_log(f"clip linked: {Path(path).name}", "status")
        elif kind == "shot_taken":
            inc_id, path = payload
            self.store.update(inc_id, screenshot_path=path)
            self.append_log(f"screenshot saved: {Path(path).name}", "status")
        elif kind == "clip_missing":
            self.append_log("no new clip appeared in the Medal folder — "
                            "check the folder in Settings and Medal's hotkey",
                            "status")
        elif kind == "scr_update":
            uid = payload
            rec = self.scr_db.get(uid, {})
            row = self.scr_rows.get(uid)
            if row and self.scr_tree.exists(uid):
                note = rec.get("note", "")
                gf = self.group_filter_var.get().strip().lower()
                matched = self._matched_groups(rec.get("groups", []), gf)
                tagged = self._note_has_filter(note)
                row.update(note=note, groups=matched, known=True)
                tags = ("tagged",) if tagged else (
                    ("matched",) if matched else ())
                self.scr_tree.item(uid, values=(
                    row["name"], uid, "✔" if tagged else "—",
                    ", ".join(matched)), tags=tags)
            self._scr_update_counts()
            n_tag = sum(1 for r in self.scr_rows.values()
                        if self._note_has_filter(r["note"]))
            pend = len(self._scr_queued)
            msg = f"{len(self.scr_rows)} in instance · {n_tag} tagged"
            if pend:
                msg += f" · checking {pend} new player(s)…"
            self.scr_info_var.set(msg)
        elif kind == "scr_tagged":
            iid = payload["iid"]
            uid = payload["uid"]
            row = self.scr_rows.get(iid)
            if row:
                row["note"] = payload["note"]
                self.scr_tree.item(iid, values=(
                    row["name"], row["user_id"] or "—", "✔",
                    ", ".join(row.get("groups", []))), tags=("tagged",))
            # persist to the DB so we remember the tag next session
            rec = self.scr_db.setdefault(
                uid, {"name": payload["name"], "note": "", "groups": [],
                      "checked_at": time.time()})
            rec["note"] = payload["note"]
            self.db.upsert_user(uid, rec)
            if self._notes_map is not None:
                self._notes_map[uid] = payload["note"]
            self._scr_update_counts()
            how = "auto-OK'd" if payload.get("auto") else "tagged"
            self.append_log(f"screening: {how} {payload['name']}'s note "
                            f"with '{payload['word']}'", "status")
            self.scr_info_var.set(
                f"{how.capitalize()} {payload['name']} '{payload['word']}'.")
        elif kind == "scr_synced":
            self._scr_status(len(self.scr_rows),
                             bool(self.vrc_api and self.vrc_api.user))
        elif kind == "scr_error":
            if payload:
                self.scr_info_var.set(payload)
                self.append_log(f"screening: {payload}", "trigger")
        elif kind == "vrc_login":
            self.vrc_status_var.set(payload)
            self.twofa_row.pack_forget()
            self.vrc_pass_var.set("")
            self._scr_sync()   # now authenticated — queue any unseen players
        elif kind == "vrc_2fa":
            label = ("authenticator app" if payload == "totp"
                     else "email code")
            self.vrc_status_var.set(f"Enter the 2FA code from your {label}.")
            self.twofa_row.pack(anchor="w", padx=16, pady=4)
        elif kind == "vrc_login_err":
            self.vrc_status_var.set(payload)
        elif kind == "status":
            self.status_var.set(payload)
            self.append_log(payload, "status")
            if payload.startswith("Listening"):
                self.stat_vars["state"].set("listening")
                self.status_lbl.configure(fg=GREEN)
        elif kind in ("stopped", "error"):
            self.running = False
            self.stat_vars["state"].set("stopped")
            self.status_lbl.configure(fg=DIM)
            if kind == "error":
                self.status_var.set(f"Error: {payload}")
                self.append_log(f"error: {payload}", "trigger")
            else:
                self.status_var.set("Stopped.")
                self.append_log("stopped", "status")
            self.start_btn.configure(text="▶  Start", style="Start.TButton")
            self.start_btn.state(["!disabled"])
            for w in (self.device_cb, self.model_cb, self.cooldown_sp):
                w.state(["!disabled"])
            self.hotkey_entry.configure(state="normal")
            self.level_smooth = 0.0

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.meter.coords(self.meter_bar, 0, 0, int(160 * self.level_smooth), 12)
        self.level_smooth *= 0.9
        if self.running:
            up = int(time.time() - self.started_at)
            self.stat_vars["uptime"].set(f"{up // 3600:02d}:{up % 3600 // 60:02d}:"
                                         f"{up % 60:02d}")
        self._poll_tick += 1
        if self._poll_tick % 16 == 0:   # ~once a second
            snap = self.watcher.snapshot()
            if snap["revision"] != self._vrc_rev:
                self._vrc_rev = snap["revision"]
                self._refresh_instance(snap)
                self._scr_sync()   # roster changed (join/leave) → resync list
        self.root.after(60, self._poll)

    def _screenshot(self, path: str) -> None:
        from PIL import ImageGrab
        self.root.update_idletasks()
        x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)
        self.on_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device")
    parser.add_argument("--model", choices=list(autoclip.MODELS))
    parser.add_argument("--autostart", action="store_true")
    parser.add_argument("--screenshot", help="save a window screenshot then exit")
    parser.add_argument("--shot-delay", type=int, default=2000)
    parser.add_argument("--tab", type=int, default=0,
                        help="tab index to open (for screenshots)")
    args = parser.parse_args()

    root = tk.Tk()
    app = App(root, args)
    if args.tab:
        app.nb.select(args.tab)
    root.mainloop()


if __name__ == "__main__":
    main()
