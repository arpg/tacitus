# Zulip for the EA (self-hosted server)

This directory runs a Zulip server on this machine for the EA's Zulip
front end. It is the official docker-zulip stack (image
`ghcr.io/zulip/zulip-server:12.3-0`, Zulip Server 12.3) cut down to one
file, published on `127.0.0.1:8080` only.

| File | Purpose |
|------|---------|
| `docker-compose.yml` | The stack: `database`, `memcached`, `rabbitmq`, `redis`, `zulip`. Committed. |
| `.env.example` | Placeholder secrets. Committed. |
| `~/.ea-private/zulip.env` | The real secrets and the owner's web password, mode 600, outside the checkout. `deploy/zulip/.env` (gitignored) is read only when this file is missing, with a warning. |
| `env.sh`, `compose.sh` | Where the env file is looked for (`ZULIP_ENV_FILE`, then the private file, then the fallback), and `docker compose` with that `--env-file`. |
| `../../scripts/zulip_bootstrap.py` | Creates the EA bot with its avatar and the private streams, lifts the edit limit, prints the config. |

All commands below run from the repo root, `/home/<you>/ea-system`. The
owner is in the `docker` group, but a shell started before that change does
not have the group, so every docker command goes through `sg docker -c`.
A fresh login shell can drop the `sg` wrapper.

## Names and URLs

- The realm lives at `http://zulip.localhost:8080`. Every name under
  `.localhost` resolves to loopback, so no `/etc/hosts` edit is needed.
- `http://127.0.0.1:8080` answers, but shows "No organization found":
  Zulip picks the realm by host name, and the only realm is on
  `zulip.localhost:8080`.
- Bare `localhost:8080` does not work. Zulip refuses an external host
  without a dot (check `zulip.E002`).
- Zulip refuses `http://` as its external scheme in production (check
  `zulip.E004`), so links the server builds (`realm_url` in
  `/api/v1/server_settings`, invitation links) say `https://`. The stack
  answers on `http://` only; clients and the EA use the `http://` URL.
- The legacy `DISABLE_HTTPS=True` and `SSL_CERTIFICATE_GENERATION`
  variables are gone in the 12.x image, and the image refuses to start
  if either is set. `CERTIFICATES: ""` is the HTTP-only mode.

## 1. Secrets

The file lives in the owner-private directory, outside the checkout:

```bash
install -m 600 deploy/zulip/.env.example ~/.ea-private/zulip.env
# Replace each change-me with the output of:  openssl rand -hex 16
```

Or generate the whole file in one step:

```bash
( umask 077
  for k in POSTGRES_PASSWORD MEMCACHED_PASSWORD RABBITMQ_PASSWORD REDIS_PASSWORD SECRET_KEY; do
    echo "ZULIP__$k=$(openssl rand -hex 16)"
  done
  echo "ZULIP_OWNER_PASSWORD=$(openssl rand -hex 12)" ) > ~/.ea-private/zulip.env
```

`ZULIP_OWNER_PASSWORD` is not read by compose. It is the owner's web
login password, kept here so the realm step below and a later login
use the same value.

## 2. Bring the stack up

```bash
deploy/zulip/compose.sh up -d
```

The first boot pulls about 5.3 GB of images, runs the database
migrations and takes one to three minutes after the pull. Wait until
the server answers:

```bash
until curl -sf -o /dev/null http://zulip.localhost:8080/api/v1/server_settings; do sleep 10; done
```

If it never answers, read the log. A configuration error ends with
"Error in the Zulip configuration. Exiting." and the check that failed:

```bash
deploy/zulip/compose.sh logs --tail 50 zulip
```

## 3. Create the realm and the owner (headless)

`manage.py create_realm` creates the organization and its owner in one
step, with no browser and no mail. The realm is "EA" on the root
domain (empty subdomain); the owner is `<your gmail>`. The
password goes in through a file inside the container that is removed
at once, so it never shows in `ps`:

```bash
grep '^ZULIP_OWNER_PASSWORD=' ~/.ea-private/zulip.env | cut -d= -f2 | tr -d '\n' |
  sg docker -c "docker exec -i -u zulip ea-zulip-zulip-1 sh -c '
    umask 077; cat > /tmp/owner-pw
    /home/zulip/deployments/current/manage.py create_realm --password-file /tmp/owner-pw \
      EA <your gmail> \"<Your Name>\"
    rc=\$?; rm -f /tmp/owner-pw; exit \$rc'"
sg docker -c "docker exec -u zulip ea-zulip-zulip-1 /home/zulip/deployments/current/manage.py list_realms"
```

The options must come before the positional arguments; argparse
rejects the email and name when they follow `--password-file`.

The browser path also works if preferred:
`manage.py generate_realm_creation_link` prints a one-time link to open
at `http://zulip.localhost:8080`. Any mail the server sends (password
resets, invitations) goes to the container log, because the stack uses
Django's console mail backend:

```bash
deploy/zulip/compose.sh logs zulip | grep -A40 'Subject:'
```

Web login: open `http://zulip.localhost:8080` and sign in with the
owner email and `ZULIP_OWNER_PASSWORD`. Chromium and Firefox treat
`*.localhost` as a secure context, so Zulip's `Secure` session cookies
work over plain HTTP here. `curl` does not, so a curl web login fails
with 403; the API is unaffected. The first login asks the owner to
accept the terms and to choose the email visibility.

## 4. The owner's API key

```bash
PW=$(grep '^ZULIP_OWNER_PASSWORD=' ~/.ea-private/zulip.env | cut -d= -f2)
OWNER_KEY=$(curl -sf http://zulip.localhost:8080/api/v1/fetch_api_key \
  --data-urlencode username=<your gmail> --data-urlencode "password=$PW" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')
```

In the web app the same key is under Settings, Account and privacy,
Manage your API key.

## 5. Bootstrap the realm for the EA

```bash
python3 scripts/zulip_bootstrap.py --site http://zulip.localhost:8080 \
  --owner-email <your gmail> --owner-key "$OWNER_KEY"
```

It creates the generic bot "EA" (`ea-bot@zulip.localhost`) with the
portrait in `assets/tacitus-avatar.png` as its avatar, the private
streams `ea`, `ea-scheduling` and `ea-dm` with the owner and the bot
subscribed (history visible to subscribers), and sets the realm's
message-edit limit to unlimited. Progress goes to stderr. Stdout is
exactly two things: the `chat` block for `~/.ea/config/ea-config.json`,
and the line `ZULIP_API_KEY=<bot key>` for the owner-private
`~/.ea-private/secrets.env`. The
script writes no file. Every step is idempotent, so a rerun reuses the
bot and prints the same key.

Verify without changing anything:

```bash
python3 scripts/zulip_bootstrap.py --site http://zulip.localhost:8080 \
  --owner-email <your gmail> --owner-key "$OWNER_KEY" --check
```

It prints OK or FAIL for the owner, the bot, each stream (private,
owner and bot subscribed) and the edit limit, and exits 1 on any FAIL.
API errors exit 2 with the call and the server's message.

A bot made before the portrait existed keeps its old picture. To give it
the portrait:

```bash
python3 scripts/zulip_bootstrap.py --site http://zulip.localhost:8080 \
  --owner-email <your gmail> --owner-key "$OWNER_KEY" --avatar
```

Applying the output (paste the `chat` block, add the secret, restart
the bot) is the switch procedure, written up in `docs/zulip-switch.md`
with the rest of the Zulip integration step.

## Resource footprint

Measured on this machine right after the first boot, idle:

| Container | Memory |
|-----------|--------|
| zulip | about 3.9 GB (Django, Tornado, queue workers in threaded mode, nginx) |
| rabbitmq | about 180 MB |
| database (PostgreSQL 14) | about 100 MB |
| memcached | about 15 MB |
| redis | about 6 MB |

Disk: about 5.3 GB of images, about 125 MB of volumes on a fresh realm.
CPU is near zero when idle. The compose file sets
`queue_workers_multiprocess = False`; the multiprocess mode needs
several more GB and gains nothing for one owner and one bot.

## Stop, start, remove

```bash
# Stop (keeps all data):
deploy/zulip/compose.sh stop
# Start again:
deploy/zulip/compose.sh up -d
# Remove the containers and network, keep the volumes:
deploy/zulip/compose.sh down
# Remove everything, the realm and all messages included:
deploy/zulip/compose.sh down -v
```

The compose project is named `ea-zulip`, so the volumes are
`ea-zulip_zulip`, `ea-zulip_postgresql-14`, `ea-zulip_rabbitmq` and
`ea-zulip_redis`, and the Zulip container is `ea-zulip-zulip-1`.
`restart: unless-stopped` brings the stack back after a reboot unless
it was stopped by hand. To take the server down for good (the serve on
8443, the ACL rule, the volumes, the backup cron, the env file and the
bot's key), follow docs/zulip-switch.md, "Uninstall Zulip".

## From dev server to the real server

The same stack becomes the owner's daily server with three changes.

1. **Host name.** Set `SETTING_EXTERNAL_HOST` to the machine's tailnet
   name (the MagicDNS name of this host), so phones and laptops on the
   tailnet reach it. Zulip keys the realm on this name, so after the
   change the root realm answers on the new name. Change
   `SETTING_ZULIP_ADMINISTRATOR` only if the address changes.
2. **HTTPS and the bind address.** Either set `CERTIFICATES:
   self-signed` (the 12.x name for the old
   `SSL_CERTIFICATE_GENERATION=self-signed`), publish `443` on the
   tailnet address only (for example `100.x.y.z:443:443`, never
   `0.0.0.0`), and accept the certificate once on each device; or keep
   HTTP on loopback and put `tailscale serve` in front, which gives a
   real tailnet certificate. In the second case set `LOADBALANCER_IPS`
   to the proxy's address as seen from the container. Never use
   Tailscale Funnel: the server stays tailnet-only.
3. **Mail.** Replace the console mail backend with real SMTP settings
   (`SETTING_EMAIL_HOST` and friends, plus a `zulip__email_password`
   secret) if password resets and invitations must reach an inbox.

Then update `zulip.site` in the EA's `chat` block and restart the bot.

**Backups.** `app:backup` writes a `pg_dump` into `/data/backups/` in
the `ea-zulip_zulip` volume daily at 03:30 UTC (the image default).
The database itself lives in `ea-zulip_postgresql-14`. Both are Docker
volumes under `/var/lib/docker/volumes`, which the `/home` and `/etc`
mirror does not cover, so take an explicit snapshot as well:

```bash
sg docker -c "docker exec ea-zulip-zulip-1 /sbin/entrypoint.sh app:backup"
sg docker -c "docker run --rm -v ea-zulip_zulip:/data:ro -v $HOME/backups:/backup alpine \
  tar czf /backup/ea-zulip-$(date +%F).tar.gz -C /data ."
```

That tarball holds the fresh dump, the uploads, the generated secrets
and the certificates, which is everything a restore needs (see the
upstream "Compose: Backups" guide for `app:restore`).

**Upgrades.** Image tags are fully pinned and there is no floating
tag. To upgrade, read the docker-zulip `CHANGELOG.md`, change the
`zulip` image tag, take a backup, then `up -d`; the entrypoint runs the
migrations. The four service images use the tags upstream's
`compose.yaml` uses (`zulip/zulip-postgresql:14`, `memcached:alpine`,
`rabbitmq:4.2`, `redis:alpine`); a PostgreSQL major upgrade needs
upstream's `upgrade-postgresql` script.

## Tailnet address and backups

The EA reaches the server at `https://<host>.ts.net:8443` through Tailscale
Serve (port 443 already carries the deck routes); `scripts/zulip_switch.sh`
configures it and points the realm at that host. `backup.sh` takes a
nightly backup with Zulip's own tool into `/home/<you>/zulip-backups`.
