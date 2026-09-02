# Running the mod suite in Docker

Step by step, from a bare Linux host to a working server. About twenty minutes,
most of it waiting for downloads.

**Status: written and reviewed, not yet built.** Docker Desktop on the
development machine fails to start (an Inference-manager socket error inside
Docker Desktop, nothing to do with these files), so the image has not been
built and run end to end. Everything here follows the deployment that is
already running under systemd — same command, same environment variables, same
paths — but treat the first `docker compose up` as the real test.

## What you need

A Linux host with Docker. On Proxmox that means **a VM, or an LXC with nesting
turned on** — Docker inside an unprivileged container without nesting fails in
confusing ways. For a container: Options → Features → tick **Nesting**, then
reboot it.

```bash
curl -fsSL https://get.docker.com | sh
```

## 1. Get the code

```bash
git clone https://github.com/minecartchris/VRC-ModTool.git /opt/modsuite
```

```bash
cd /opt/modsuite && git checkout web
```

## 2. Make the two folders that hold everything

Nothing inside the image is written to at runtime. All state lives in these,
which is what makes an upgrade "pull a new image" rather than a migration.

```bash
mkdir -p /opt/modsuite/data /opt/modsuite/config
```

The container runs as uid **10001**, so it has to be able to write `data/`:

```bash
chown -R 10001:10001 /opt/modsuite/data
```

## 3. Write the config

`/opt/modsuite/config/web_config.json`:

```json
{
  "staff_group": "grp_your_staff_group_id",
  "vrc_contact": "you@example.com",
  "sync_token": "a-long-random-string",
  "root_admins": ["usr_your_vrchat_id"],
  "audit_group": "grp_7112d2b5-7a61-4ce0-8d1e-2285a4f37421",
  "roster_group": "grp_7112d2b5-7a61-4ce0-8d1e-2285a4f37421",
  "discord_webhook_url": "",
  "https_only": false,
  "auto_reload": false
}
```

Three of those decide whether it works at all:

- **`vrc_contact` must be a real address you monitor.** VRChat puts it in the
  User-Agent and blocks the API with `waf_code 13799` when it is missing or
  fake. Sign-in fails and the cause is not obvious.
- **`staff_group`** is the VRChat group whose members may sign in. Without it
  nobody can, including you.
- **`https_only`** marks the session cookie Secure. Leave it `false` while you
  are testing over plain `http://` on the LAN, and turn it **on** once there is
  TLS in front — otherwise the cookie is sent in the clear.

`sync_token` is for the desktop client and roster agents; any long random
string. Generate one with `openssl rand -hex 32`.

## 4. Start it

```bash
cd /opt/modsuite && docker compose up -d --build
```

```bash
docker compose ps && docker compose logs -f modsuite
```

Healthy looks like `Uvicorn running on http://0.0.0.0:8787` followed by
`[boot] started, pid 1`. Then:

```bash
curl -s http://localhost:8787/healthz
```

Open `http://<host-ip>:8787` and sign in with a VRChat account that is in the
staff group.

## 5. Put TLS in front of it

Sign-in sends VRChat credentials, so do not expose port 8787 to the internet
directly. Either a Cloudflare tunnel pointed at `http://<host-ip>:8787`, or a
reverse proxy terminating TLS. Once that is up, set `"https_only": true` in the
config and `docker compose restart modsuite`.

## Moving an existing server in

If you already have the systemd deployment, the database moves as a file — but
copy it while nothing is writing, or take a proper snapshot:

```bash
systemctl stop modsuite
```

```bash
cp /var/lib/modsuite/modtool.db* /opt/modsuite/data/ && cp /etc/modsuite/web_config.json /opt/modsuite/config/
```

```bash
chown -R 10001:10001 /opt/modsuite/data && docker compose up -d --build
```

Copy the `-wal` and `-shm` files too — that is what the `modtool.db*` glob is
for. The database is in WAL mode, and the `.db` on its own can be missing the
most recent writes.

Leave the old service stopped but installed until you are satisfied. Two
copies of this server running against two databases will happily both accept
kick logs, and merging them afterwards is not a thing you want to do.

## Day to day

**Logs** — `docker compose logs -f modsuite`, or `--tail 200`.

**Restart** — `docker compose restart modsuite`. The container is given 30
seconds to stop, which suits the app: uvicorn stops waiting for open
connections after ten, and the run log records the shutdown before it goes.

**Upgrade**:

```bash
cd /opt/modsuite && git pull && docker compose up -d --build
```

Schema migrations run themselves at startup — new columns are added to the
existing database, nothing is rewritten. Take a backup first anyway.

**Backup** — everything that matters is `data/`, and the safe way is SQLite's
own backup rather than a copy taken mid-write:

```bash
docker compose exec modsuite python -c "import sqlite3,time; s=sqlite3.connect('file:/data/modtool.db?mode=ro',uri=True); d=sqlite3.connect('/data/backup-'+time.strftime('%Y%m%d-%H%M%S')+'.db'); s.backup(d); d.close(); print('done')"
```

Then copy that file off the host. A backup on the same disk as the original is
not a backup.

## The status page

The second container serves a read-only status page on **8090**, deliberately
separate so there is still something to ask when the tool itself is down.

One honest caveat: some of what it reports comes from `systemctl` and
`journalctl`, which do not exist in a container. Service state, memory and
restart counts will be blank there. The database counts, the queue depth, the
agent list and the health check all work. If you want the full picture, run
`ops/watchdog.py` on the Docker host instead — it watches from outside, which
is the only place a container's own death is visible.

Don't want it? `docker compose up -d modsuite` starts the tool alone.

## When it does not work

**Nothing on 8787.** `docker compose ps` — if the container is restarting,
`docker compose logs modsuite` will have the traceback. A bad `web_config.json`
(trailing comma, smart quotes) is the usual one.

**"unable to open database file"** — the volume permissions. The container is
uid 10001 and the folder is probably owned by root:

```bash
chown -R 10001:10001 /opt/modsuite/data
```

**Sign-in fails with a 403 from VRChat.** `vrc_contact` is missing or not a
real address. See step 3.

**Everyone is refused at sign-in.** `staff_group` is wrong, or the account is
not in it. An admin listed in `root_admins` can get in either way — that is
what the list is for.

**The times are wrong.** Set `TZ` in `docker-compose.yml`; it defaults to
`America/Chicago`.

**Kick prompts never appear.** `audit_group` must be set, and reading the audit
log borrows a signed-in moderator's VRChat permissions — somebody holding
`group-audit-view` has to have signed in since the last restart.
