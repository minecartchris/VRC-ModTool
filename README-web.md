# Web app + sync server

A browser front end for the parts of the mod suite that aren't tied to your
gaming PC: **incident logs** and **age checks**. The desktop app keeps doing the
things only it can do — listening to voice chat, pressing the Medal hotkey,
screenshotting VRChat, reading the local log — and syncs its records here.

Sign-in is your own VRChat account, and access is decided by **staff-group
membership**: if VRChat says you're in the configured group, you're a
moderator here. Remove someone from the group and they lose access.

## Quick start (same PC as the desktop app)

```bash
python run_web.py --init
```

That writes `web_config.json` with a generated `sync_token`. Edit it and set:

| Key | What to put |
|---|---|
| `staff_group` | your moderator group's `grp_` ID, short code, or part of its name |
| `vrc_contact` | a real email or Discord handle — VRChat's API 403s without it |

Then start it:

```bash
python run_web.py
```

Open <http://127.0.0.1:8787> and sign in with your VRChat account (2FA
supported). It reads and writes the same `modtool.db` as the desktop app, so
your existing incidents are there immediately — no import step.

## Connecting the desktop app

On the desktop **Settings** tab, under *Server sync*:

1. tick **Sync with server**
2. **Server URL** — `http://127.0.0.1:8787`, or wherever you host it
3. **Token** — the `sync_token` from `web_config.json`
4. **Save settings** (or **Sync now** for a one-shot round)

The status line shows `Synced HH:MM:SS — N up, M down`. The desktop also
publishes its current world and roster, which is what fills the web
**Screening** page.

Running the server on the same machine as the desktop app? Both processes open
the same SQLite file in WAL mode, so they already share everything and sync is
optional. It matters when the server lives elsewhere, or when a second
moderator's PC files incidents into the same set.

## Where the roster comes from

**VRChat's API cannot tell you who is in an instance.** It gives a headcount
and nothing else — verified against a live 48-player group instance:

| Endpoint | What comes back |
|---|---|
| `/instances/{world}:{instance}` | `n_users: 48`, no `users` field |
| `/groups/{gid}/instances` | `memberCount` only |
| `/worlds/{wid}/{iid}` | `n_users: 48`, no list |
| `/auth/user/friends` | locations, but only for friends — 4 of the 48 |

The names exist only in the output log of a client that is *in* the instance.
So the roster always comes from something running on a PC that is in the world:

1. **This server**, if it runs on that PC — it tails the log itself. Nothing
   else to install, desktop app not required.
2. **The roster agent** (`agent.py`), for the normal hosted case. See below.
3. **The desktop app**, which pushes its roster when sync is on.

**One screening list per instance.** A paired agent reports the room its own PC
is in, and rosters are grouped by `world_id:instance_id` — not by who sent
them. Screening shows the room you are standing in, which is the one you need;
mixing a colleague's instance into it is how somebody gets screened against a
list they were never on.

Two moderators in *different* rooms therefore get different lists. Two in the
*same* room get one merged list, and both count as its owner. The merge is a
union rather than a most-recent-wins, because their logs genuinely differ: a
client only learns about a join it rendered, so somebody who arrived behind you
may be missing from your log and present in theirs. Players are matched on
`usr_` id, falling back to display name when a log line carried no id.

Where there is more than one live, a switcher across the top names each room and
everyone reporting it, so you can look into a colleague's instance deliberately.
A moderator with no agent of their own — screening from a phone, say — is asked
which room rather than handed an arbitrary one; if only one is live, that is not
a choice worth asking about, so they simply get it.

Reporters heartbeat every 30s; a heartbeat updates liveness but deliberately
does *not* count as a change, so open browsers don't reload twice a minute for
nothing. The reload poll is scoped to the instance on screen, so a join in a
world you are not looking at leaves your page alone — while a second agent in
*your* room still refreshes you, since they share a scope.

If no reporter is current the Screening page says so in red rather than quietly
showing an old list — screening against people who already left is worse than
showing nothing, and that page is where verdicts get recorded.

Everything else — incidents, age checks, reports, search — works with no
reporter at all, from any browser.

## The roster agent

For a hosted server, one moderator who is in the instance runs this:

```bash
python agent.py --server https://mods.example.com
```

(or `agent.bat --server ...` on Windows, then just double-click it afterwards —
settings are remembered in `agent_config.json`). It asks to be paired on first
run; `--pair` sets it up again after a key is revoked.

It tails the VRChat log and POSTs the roster. That is all it does: **no Vosk
model, no audio capture, no Tkinter, no clipping** — its only dependency is
`requests`. One person running it gives every browser a live Screening page,
including moderators on phones who have nothing installed.

No token is needed: the first run pairs through the panel.

Set `"read_local_log": false` to stop the server reading its own log (it is
skipped automatically where there is no VRChat log directory, e.g. a Linux
host, so a hosted deployment needs no configuration).

## Code changes and staying signed in

The server watches its own source and restarts when a `.py`, `.html`, `.css`
or `.js` file changes, so edits apply without you touching the terminal.
Templates were always hot; Python is now too.

This is a supervisor that replaces the whole process, not uvicorn's
`--reload`. On Windows uvicorn's worker restart hangs: it logs "Reloading…",
the replacement worker never starts, and the *old* process keeps serving — so
edits look applied while the running code never changes. A full process
restart costs about a second and cannot half-succeed. `/healthz` reports
`started_at` so you can always tell whether a restart really happened.

**A restart no longer signs anyone out.** The session survives, and so does the
live VRChat connection behind it — no "no live VRChat connection" banner, no
re-entering credentials and 2FA to tag one note.

That works because the VRChat auth cookie is stored, encrypted, rather than
held in memory. The scheme:

| In the database | Only in your browser |
|---|---|
| `sha256(session token)` | the session token itself |
| VRChat cookie encrypted with a key derived from the token | — |

So `modtool.db` on its own decrypts nothing. Someone who copies the file gets
neither a usable session nor anybody's VRChat login; they would also need the
cookie out of a signed-in browser. This is stronger than what the desktop app
does, which keeps its cookie in plain text in `vrc_cookies.txt`.

Turn the watcher off for a hosted deployment, where it is just overhead:

```json
"auto_reload": false
```

or run `python run_web.py --no-reload`. Note the watcher runs the app in a
child process, so if you kill the server from a script rather than Ctrl+C,
the child notices the supervisor is gone and exits on its own within a couple of seconds.

## Look and feel

The UI follows the Teen Chillout Mod console (`team-chillo-mod-tool`): the same
Material 3 dark palette, the 240px navigation rail, the 64px top bar and the
same card, chip and metric-tile shapes. Tokens are copied from that project's
`globals.css`, so changing one theme and porting the values keeps the two
tools looking like one suite.

Two deliberate differences: fonts and icons are **not** fetched from Google.
This server handles records about minors on a LAN that may have no internet,
so it makes no third-party requests — icons are inline SVG
(`templates/_icons.html`), and Hanken Grotesk leads the type stack so it is
used wherever it happens to be installed, falling back to the system UI font
instead of being downloaded. An icon *font* that fails to load renders its
ligature as raw text, which would leave the nav reading "dashboard flag group"
in words.

## Who is a moderator

Two places show it:

- **The signed-in user** carries a `MOD` badge in the top bar. Everyone who can
  sign in has passed the staff-group check, so it is always earned; hover it to
  see which group granted access.
- **Players in the roster** get a `MOD` badge on the Screening page when they
  are in the staff group, using the same match as sign-in. So the badge means
  exactly "this person could log into this tool" — handy for not wasting a
  screening pass on a colleague, and for seeing at a glance whether any staff
  are present in a bad instance.

## Live updates

Pages refresh themselves, so an open browser tracks the instance the way the
desktop app does. Every few seconds the page asks `/api/state` for a
fingerprint of the record set and reloads if it moved — someone joining the
instance, another moderator filing a check, the listener catching a trigger.

Two things keep that from being annoying:

- **It never reloads while you're mid-entry.** If any field has something typed
  in it — a half-entered age, an unsaved note — the page shows a *New activity*
  pill at the bottom instead and waits for you to click it. Reloading between
  someone typing an age and clicking a verdict button is how you file the wrong
  verdict on the wrong player.
- **Background tabs don't poll**, and a tab refreshes the moment you switch back
  to it.

The **live / paused** button in the header turns it off; the choice is
remembered in that browser.

## Filtering the roster

The Screening page has a chip for each state, with counts:

| Chip | Who it shows |
|---|---|
| Everyone | the whole instance |
| **Not verified** | **anyone not cleared — unchecked, under range, or over range** |
| Verified | cleared by an in-range check, or already carrying the note tag |
| Unchecked | nobody has looked at them yet |
| Under range / Over range | their latest verdict |

*Not verified* is deliberately broader than *Unchecked*: a player marked under
or over range has been looked at but is not cleared, and folding them in with
the verified crowd is how people get missed. Unverified rows also carry a red
edge marker in the full list. Chips with a count of zero are hidden.

A player counts as verified two ways — an in-range check recorded here, or the
verification word already in your VRChat note. That second path is what the
desktop app has always used, including for auto-verified staff-group members,
so the two front ends agree on who is cleared.

## Hosting it for a team

```bash
pip install -r requirements-web.txt   # no audio stack; installs on Linux
python run_web.py --host 0.0.0.0
```

- **Put it behind HTTPS.** Sign-in sends VRChat credentials to your server,
  which forwards them to VRChat. Over plain HTTP on a network you don't control
  that is a credential leak. Terminate TLS with Caddy/nginx and set
  `"https_only": true` so the session cookie is marked Secure.
- Move the database with `MODTOOL_DB=/var/lib/modsuite/modtool.db`.
- Clips and screenshots stay on the PC that captured them. The server only
  serves media under `incident_shots/` plus anything you list in
  `media_roots` — paths outside those are refused, because incident records
  arrive over the sync API and must not be able to name arbitrary files.

## What the server stores

| Table | Contents |
|---|---|
| `incidents` | one row per trigger or manual filing; transcript, roster, world, clip/screenshot paths, status, notes |
| `age_checks` | one row per verdict: player, over/under/in-range, reported age, who checked, which incident it filed |
| `screening_users` | cached VRChat note + groups per user, so nobody is looked up twice |
| `rosters` | one row per reporter: the instance that PC is in, and whose agent it is |
| `web_sessions` | opaque session tokens; **no passwords, no VRChat cookies** |
| `agent_keys` | one roster key per paired PC, revocable on its own |
| `agent_pairings` | short-lived pairing codes, single-use |
| `known_users` | everyone who has signed in, so admins can be appointed later |
| `admins` | who can appoint admins, revoke any agent, and edit or delete a log |

Your VRChat auth cookie lives in server memory only, for as long as the process
runs. Restart the server and you stay signed in for browsing records, but
writing VRChat notes needs a fresh sign-in.

## How sync works

Both sides run the same `db.py`. Every record carries `updated_at` and a
`deleted` tombstone, and `upsert_*` bumps `updated_at` **only when content
actually changes**. That single rule is what makes the loop terminate: a record
pulled from the server and written locally is byte-identical, so it never
re-enters the outbound queue.

Each round is pull-then-push against a per-peer cursor in `sync_state`.
Conflicts are last-write-wins, which is safe in practice because the two sides
edit different fields — the desktop writes new incidents and clip paths, the
web writes statuses, notes and age checks.

## Routes

| Route | Purpose |
|---|---|
| `/` | counts, live instance, recent activity |
| `/incidents`, `/incidents/{id}` | list/search, detail with evidence, notes, status, paste-ready report |
| `/age-checks` | log + record a check (over/under also files an incident) |
| `/screening` | live roster, filterable by verification state, with per-player verdict buttons and VRChat note tagging |
| `/settings` | your account, the agent download, your paired PCs |
| `/admin` | admins, every paired agent, and revoking any of them |
| `/pair/{code}` | approve an agent that is asking to report as you |
| `/api/agent/pair/*` | the agent's side of that: start, then poll |
| `/api/state` | change fingerprint polled by open pages |
| `/api/sync/push`, `/api/sync/pull`, `/api/sync/roster` | desktop sync; `X-Sync-Token` header |
| `/healthz` | liveness |

## Security notes

- Login is rate-limited per IP (8 attempts / 5 min) — this endpoint proxies to
  VRChat, so it must not become a brute-force tool.
- Session cookies are `HttpOnly` + `SameSite=Lax`, which is what blocks
  cross-site form posts; there is no separate CSRF token.
- `sync_token` grants full read/write to every record. Treat it as a password,
  and don't reuse it as the staff group.
- Age-check records are notes about real people, including minors. Keep the
  server private and the database off shared drives.

## Deploying on Proxmox

This runs happily as an unprivileged LXC container. Substitute your own
addresses for `$PVE_HOST` (the Proxmox node) and `$CT_IP` (the container) —
the real ones are deliberately not in this public repo.

Running as LXC **101 `modsuite`**:

| | |
|---|---|
| Address | `http://$CT_IP:8787` (static) |
| Resources | 2 cores, 512 MB RAM, 4 GB rootfs on `local-lvm` |
| Service | `systemctl {status,restart} modsuite` — enabled, `onboot=1` |
| Code | `/opt/modsuite/app` (venv at `/opt/modsuite/venv`) |
| Database | `/var/lib/modsuite/modtool.db` |
| Config | `/etc/modsuite/web_config.json` |
| Logs | `journalctl -u modsuite -f` |

A container rather than a VM: check `lvs` first, because a thin pool that is
already most of the way full will not survive an overcommitted VM disk, and
that takes the *other* guests down with it. A Python web app gains nothing
from full virtualisation anyway.

`https_only` is on, which marks the session cookie Secure — correct behind the
tunnel, but it means **signing in over plain `http://$CT_IP:8787` will
not work**; the cookie is refused. Set it to `false` temporarily if you need to
test on the LAN before the tunnel is up.

### Cloudflare tunnel

Point the tunnel's origin at `http://$CT_IP:8787` (or `127.0.0.1:8787`
if you run `cloudflared` inside this container). Nothing needs to be
port-forwarded — the tunnel dials out.

### Updating the deployment

```bash
git archive --format=tar.gz -o /tmp/modsuite.tar.gz web
scp /tmp/modsuite.tar.gz root@$PVE_HOST:/tmp/
ssh root@$PVE_HOST 'pct push 101 /tmp/modsuite.tar.gz /tmp/a.tar.gz &&
  pct exec 101 -- bash -c "tar -xzf /tmp/a.tar.gz -C /opt/modsuite/app &&
  chown -R modsuite:modsuite /opt/modsuite && systemctl restart modsuite"'
```

### What this deployment cannot do

- **No roster on its own.** It is not on a PC in VRChat, so Screening stays
  empty until someone runs `agent.py` (see above). The pages say so.
- **No clips or screenshots.** They live on the gaming PC; `serve_media` is
  off. Incident pages show the recorded path instead of the file.

## Packaging the agent as an .exe

```bash
python build_agent.py --server https://vrcmod.example.cc
```

Produces `dist/VRChatRosterAgent.exe` (~15 MB) with the server URL compiled in
and **no credential at all** — it pairs on first run (above), so a leaked build
is worth nothing. A moderator downloads one file, double-clicks it, opens the
link it prints, and leaves it running while they are in the instance. It writes
`agent_config.json` next to itself so the settings survive restarts.

`--token ROSTER_TOKEN` still bakes one in for the old double-click-and-go
behaviour, at the cost of shipping a shared secret inside a binary.

Windows SmartScreen warns on first run because the binary is unsigned:
*More info → Run anyway*.

### Use the roster token, never the sync token

Anyone holding the .exe can pull strings out of it, so treat whatever is baked
in as public. That is why the server has two secrets:

| Token | Accepted on | Can do |
|---|---|---|
| `sync_token` | all of `/api/sync/*` | read and write every incident and age check |
| `roster_token` | `/api/sync/roster` only | report who is in an instance, nothing else |

Build with `roster_token`. A leaked agent then costs you a bogus roster, not
your moderation records. Verified: the roster token gets 200 on `/roster` and
401 on both `/pull` and `/push`.

Better still, bake in neither and let each PC pair itself — same scope, but the
roster carries the moderator's name and either of you can revoke that one PC.

## Importing the Teen Chillout Firestore history

The web tool (`team-chillo-mod-tool`) keeps its history in Firestore:
`kick_logs` (moderation actions) and `allowed_users` (the moderator allowlist
with Mod/HR roles). `import_teenchillout.py` pulls both in over the sync API,
so Firebase credentials never touch the server:

```bash
python import_teenchillout.py --service-account sa.json \
    --server https://vrcmod.example.cc --token SYNC_TOKEN --dry-run
```

Drop `--dry-run` to apply. Mapping:

| Firestore | Mod Suite |
|---|---|
| `kick_logs` document | an incident, `origin: teenchillout`, status `reported` |
| `kickedUsersData[].link` | the player's `usr_` id, parsed out of the profile URL |
| reason containing *overage* | an age check per target, verdict **over** |
| reason containing *underage* | an age check per target, verdict **under** |
| `Overage - 20` | `reported_age: 20` — only when the number is attached to the verdict word, so prose like "sounded 12 and said he was 19" isn't guessed at |
| `allowed_users` | the `staff` table; HR/Mod shows on the badge |

Re-running is safe: ids are derived from the Firestore document id and
`upsert_*` only counts a write when content changes, so a second run reports
zero rather than duplicating.

Access is still decided by VRChat staff-group membership — the imported roles
are displayed, not enforced. Gating destructive actions on HR is a deliberate
non-change; it would need whoever runs this to actually hold that role.

## Asking why: audit-log kicks and warns

VRChat records *that* a moderator kicked someone, never *why*. The server polls
the group audit log and queues each action so its moderator is asked for a
reason while they still remember it.

```json
"audit_group": "grp_…",          // the group whose instances you moderate
"audit_poll_seconds": 60,
"discord_webhook_url": "…",      // where the finished log is announced
"overaged_webhook_url": "…"      // second channel for age removals
```

| Audit event | Becomes |
|---|---|
| `group.instance.kick` | a **Kick** awaiting a reason |
| `group.instance.warn` | a **Warn** awaiting a reason |

The moderator sees a prompt on whatever page they're on — *"Why did you kick
X?"* — plus a count in the nav. Answering picks reasons from chips (the same
taxonomy as the web tool) with a detail box, and produces:

- an incident, credited to whoever VRChat recorded as the actor, not whoever
  filled the form in;
- an age check when the reason mentions overage/underage, using the *same*
  rules as the Firestore import so the two can't disagree — a bare number in
  the detail box is read as the age;
- a Discord embed byte-compatible with the web tool's, so both can post to one
  channel.

Polling borrows a signed-in moderator's live VRChat session, so there is no
extra account or stored credential. It needs the **`group-audit-view`**
permission on that group. Without it VRChat answers 403 and `/pending` says so
outright rather than sitting there looking healthy.

Only the first hour of history is queued on first run, so switching this on
doesn't confront somebody with every kick the group ever had.

## Kick Log

`/kick-log` files a kick, warn or ban by hand — the page the old tool had, for
when nobody with audit access is around. Multiple players per log, a profile
link resolves to a display name using your own VRChat session, and the shared
reason chips are the same 15 the web tool offers so the two stay comparable.

Submitting produces exactly what the audit prompt produces — one incident, an
age check if the reason mentions overage/underage, and the Discord embed —
because both call the same helper.

**Personal shortcuts.** Anyone can add reason chips to their own account; they
appear on the Kick Log and the audit prompt, and nobody else sees them. Stored
in `user_reasons`, keyed by VRChat user id.

## Your settings, and personal roster keys

Clicking your own name in the top bar opens `/settings`: which staff group let
you in, the roster agent to download, and the PCs reporting as you.

**There is no allowlist.** Access is staff-group membership and nothing else —
the page says so, because the tool this replaced (`team-chillo-mod-tool`)
refused people with *"your VRChat account is not on the admin allowlist"* and
that error still gets reported here. The `staff` table this tool imports is
displayed, never enforced.

**Getting the agent.** The page serves the packaged agent when the server has a
build to hand out:

```json
"agent_exe": "/opt/modsuite/agent/VRChatRosterAgent.exe"
```

Empty falls back to `dist/VRChatRosterAgent.exe` next to the code, which is
where `build_agent.py` leaves it. Absent, the page says so and shows the
command to run it from a checkout instead of offering a link that 404s. The
download needs a session — it is a build of our own client, pointed at this
server, so there is no reason for it to be public.

### Setting an agent up without handling a key

A key that gets copy-pasted ends up in a Discord message. So the agent asks for
one itself, and the moderator only ever handles a link:

1. run the agent — it prints a link and a short code, and opens the link;
2. the moderator opens it in a browser where they are already signed in;
3. opening it *is* the approval. The key comes back down the agent's own
   connection, and it starts reporting.

Nothing has to be read off a screen, so nothing can be photographed, streamed
or pasted into a channel by mistake.

| Endpoint | Who | What |
|---|---|---|
| `POST /api/agent/pair/start` | the agent, no credential | returns `code`, `secret`, `url` |
| `GET /pair/{code}` | a signed-in moderator | approves it, on the spot |
| `POST /api/agent/pair/poll` | the agent, with `secret` | `pending`, then the key, once |

The code is short and unambiguous (no I, O, 0 or 1 — it gets read aloud), lasts
10 minutes, and is single-use. Seeing one is not enough to steal the key:
collecting it needs the `secret` only the agent holds, and approving it needs a
staff session. `pair/start` is rate limited per IP because it is necessarily
unauthenticated.

The approval page says who it just authorised and offers **Undo — I didn't
start this**, which declines it and pulls the key back if it has already been
collected. That is the answer to a link arriving from somebody else.

### One key per PC

| | `sync_token` | `roster_token` | a paired key |
|---|---|---|---|
| Read and write every record | yes | no | no |
| Report an instance roster | yes | yes | yes |
| Names who is reporting | no | no | **yes** |
| Revocable per machine, in one click | no | no | **yes** |

The agent otherwise reports its PC's hostname, and `DESKTOP-4F9K2` on the
Screening page tells nobody who to ask about the roster. A paired key makes it
read as the moderator who is actually in the instance.

Desktop and laptop get a key each, listed on the settings page with when they
last reported, so revoking one leaves the other running. Revoking takes effect
on the agent's next heartbeat, about 30 seconds; that agent then prints what
happened and how to pair again. Keys live in `agent_keys` in the clear —
pairing means they normally never leave the two machines, and one grants
strictly less than the session cookie already in that browser.

**Setting one up by hand** is still there, folded away, for a PC you cannot
open a browser on: mint a key, paste it in. That is the path pairing exists to
avoid, so it is not the default.

## Admins

A second, smaller list on top of being a moderator. Moderating needs nothing
from it; it exists for the handful of things that should not be everybody's.

| An admin can | Anybody else |
|---|---|
| Appoint and remove other admins | — |
| Revoke **anybody's** roster agent | revoke their own |
| Edit a kick, warn or ban log | — |
| Delete an incident | mark it *dismissed* |

Deleting used to be open to every moderator. It is an admin action now: a
record of a real moderation action should not disappear because somebody
mis-clicked, and *dismissed* already exists for "this one doesn't matter".

**Editing is recorded, not hidden.** A correction rewrites the action, reason
and players, and appends to the log's own transcript what it said before and
who changed it. The report text carries that with it, so a corrected log can
never quietly disagree with a screenshot somebody already took.

**Who can be appointed:** anyone who has signed in here at least once, picked
from a dropdown. Signing in at all requires the staff group, so every candidate
is already a moderator; and the tool having seen the account is what proves the
id is real. The list comes from `known_users`, which is written on every
sign-in and — unlike `web_sessions` — never purged.

**Root admins** come from the config and cannot be removed in the UI:

```json
"root_admins": ["usr_…"]
```

That is the backstop against the last admin removing themselves and locking
the tool's administration out of its own settings.

## Audit access is per-account

The audit log is read with each **moderator's own** VRChat permissions, not a
service account. The watcher tries every signed-in moderator in turn, skips the
ones VRChat refuses, and remembers whichever worked so the steady state is one
call per poll. `/pending` names whose permissions are being used.

So the feature switches on whenever someone holding `group-audit-view` signs in
— usually senior staff — and everyone else benefits without needing the
permission themselves. When nobody eligible is signed in, `/pending` says so
and points at the Kick Log instead.

## Player pages

Clicking a name on Screening, an age-check row or an incident roster opens
`/player/{usr_id}` — everything known about one person in one place.

**From VRChat** (`GET /profile/{userId}`, read with your own session): bio,
**pronouns**, profile icon, trust rank derived from `trustTags`, languages,
represented group, bio links, and VRChat's own `ageVerified` /
`ageVerificationStatus`.

That last one is shown *separately* from this tool's age checks and never
merged with them: VRChat verifying an adult is a different claim from a
moderator judging someone in-range for a teen group, and collapsing the two
would lose the distinction that matters.

**From this database**: every incident naming them, every age check with who
recorded it, your cached VRChat note, their groups, and their Mod/HR rank if
they're on the allowlist. You can record a verdict without leaving the page.

Two deliberate details:

- **Name-only matches are listed apart.** Records imported from before user ids
  were captured can only be matched on display name, which is not proof of
  identity — VRChat display names are changeable and reusable, so those appear
  under "possible matches" rather than as fact.
- **The page never depends on VRChat being up.** If the API is slow, the
  session is stale, or the id is malformed, the profile section says so and the
  history — the part that matters — still renders.

Bio links are shown as their full URL and carry `rel="noopener noreferrer
nofollow"`: they are attacker-controlled strings, so a moderator should see
where one goes before deciding to follow it.
