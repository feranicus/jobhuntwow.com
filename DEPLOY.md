# JobHuntWOW ("Electronic") — how www.jobhuntwow.com is built and deployed

**Single source of truth. Everything below is code in this repo; GitHub builds it and ships it to the
droplet. Nothing is built on the droplet. Nothing on the droplet is hand-edited.**

## The one command

```bash
cd "C:\Python SW\Linkedin Scraper\jobhuntwow-app"
python ship_web.py
```

That does all of it: preflight (git + `gh` + repo/remote, bootstrapping them if missing) -> refuse to
push if a secret file got staged -> commit + push -> trigger and stream the deploy workflow -> verify
`https://www.jobhuntwow.com/api/health` returns `200`. It is idempotent: re-run it any time.

Requirement, once: the GitHub CLI installed and logged in (`gh auth login`).

## Architecture (one host, one image)

```
Browser --HTTPS--> www.jobhuntwow.com  (A-record -> droplet)
                      |
              shared Caddy container   (already owns :80/:443, auto-TLS via Let's Encrypt)
                      |  reverse_proxy jhw-web:8000     <- deploy/caddy/jobhuntwow.caddy
                      v
                  jhw-web   ONE container: FastAPI backend + the built React SPA
                            (Dockerfile.web, uvicorn serve:app, non-root, no published port)
```

- **One image, not two.** `Dockerfile.web` stage 1 builds `frontend/` with vite; stage 2 runs the
  FastAPI app and serves `dist/` from the same process. There is no nginx container in production and
  no `/api` proxy hop inside the pod: `/api/*` routes win over the SPA catch-all by registration order.
- **No published ports.** `docker-compose.web.yml` publishes nothing. The only way in is the shared
  Caddy, over the shared Docker network, by container name.
- **One network.** `jhw-web` is defined in exactly one compose file on exactly one network. A container
  on two networks makes Docker DNS hand the proxy an unreachable IP -> intermittent 502. The deploy
  asserts the container is on exactly ONE network and fails if not.
- **Isolation.** VideoDead / Amnezia / colt-stack are never modified. Our vhost is a separate block
  delimited by `# jhw:jobhuntwow BEGIN/END` markers.

## The pipeline (`.github/workflows/web-deploy.yml`)

Runs on push to `main` touching `backend/**`, `frontend/**`, `Dockerfile.web`,
`docker-compose.web.yml`, `deploy/caddy/**`, or the workflow itself - or via "Run workflow".

1. **build** (`permissions: packages: write`) - GitHub builds `Dockerfile.web` and pushes
   `ghcr.io/<owner>/jhw-web:latest` and `:<sha>` to GHCR, labelled
   `org.opencontainers.image.source` so GHCR links the package to the repo (no "make public" click
   needed; the deploy also `docker login`s with the workflow token).
2. **deploy** (`permissions: packages: read`) - optional Tailscale hop, then SSH to the droplet to:
   - `scp` `docker-compose.web.yml` + `deploy/caddy/jobhuntwow.caddy` to `/opt/jobhuntwow/`,
   - write `/opt/jobhuntwow/.env` (chmod 600) from the `JHW_ENV_B64` secret, or keep the existing one
     and fail loudly if there is none,
   - `docker compose -p jobhuntwow -f docker-compose.web.yml pull && up -d --force-recreate`
     (**never** the remove-orphans flag - it has deleted live sibling services in this fleet),
   - strip the `# jhw:jobhuntwow BEGIN/END` block from the shared Caddyfile, re-append the committed
     snippet, `caddy validate`, then `caddy reload --force` (restores the backup and fails if validate
     fails, so a bad config can never take the site down),
   - verify: image, single network, and the real proxy hop `caddy -> jhw-web:8000/api/health`
     (**fatal** if it fails), then the public vhost (**warning only** - it depends on the DNS step).

Every `ssh`/`scp` carries `-o ConnectTimeout=10 -o BatchMode=yes -o ServerAliveInterval=15
-o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new`. A missing timeout once hung a deploy
for 40 minutes.

Rollback: re-run the workflow from an older commit, or set `WEB_TAG=<sha>` in `/opt/jobhuntwow/.env`
and re-run. The `jhw_data` volume persists across image changes.

## What you must set (once)

### GitHub secrets - Settings -> Secrets and variables -> Actions -> *Secrets*

| Secret | Required | What it is |
|---|---|---|
| `DROPLET_SSH_KEY` | **yes** | Private SSH key (PEM, whole file) that can log into the droplet. |
| `DROPLET_HOST`    | yes*    | Droplet address for SSH, e.g. `64.225.108.200`. *Not needed if you set the `TAILNET_HOST` variable. |
| `DROPLET_USER`    | no      | SSH user. Defaults to `root`. |
| `JHW_ENV_B64`     | **yes** (first deploy) | **Base64 of your runtime `.env`** (`DO_INFERENCE_KEY`, `AGENT_PROXY_TOKEN`, `QWEN_MODEL`, ...). It is written to `/opt/jobhuntwow/.env` chmod 600 and never echoed. Omit it later to keep whatever is on the droplet. |
| `TS_AUTHKEY`      | no      | Tailscale auth key, only if the droplet's `:22` is not reachable from GitHub runners. The step is skipped when empty. |

`GITHUB_TOKEN` is built in - you do not create it.

Make the base64 (do not paste the raw file into the secret box):

```bash
# macOS/Linux:  base64 -w0 .env
# Windows PowerShell:
[Convert]::ToBase64String([IO.File]::ReadAllBytes(".env"))
```

### GitHub variables - same page, *Variables* tab (optional)

| Variable | Default | What it is |
|---|---|---|
| `TAILNET_HOST` | (unset) | Tailscale IP of the droplet; used instead of `DROPLET_HOST` when set. |
| `JHW_NETWORK`  | `videodead_appnet` | The **existing external** Docker network the shared Caddy is on. |

### DNS - the ONE irreducible human step

At the registrar for `jobhuntwow.com`:

```
A    @      ->  <droplet public IP>      (64.225.108.200 at the time of writing)
A    www    ->  <droplet public IP>
```

Only the domain owner can do this; no script can mint DNS credentials. Until both records point at
the droplet, Caddy cannot issue a certificate and `https://www.jobhuntwow.com` will not answer - the
deploy still succeeds and prints a warning, and `ship_web.py`'s final check will report it.

**Remove any GitHub-Pages A-records (185.199.108-111.153) and the `www` CNAME to GitHub.** A domain
half-claimed by GitHub Pages while DNS caches expire is exactly the 404/502 flip-flop that cost days
on the sibling project.

## Known collision to check before the first deploy

The droplet already serves the **static** `jobhuntwow.com` one-pager through the same shared Caddy
(legacy `deploy_jobhuntwow_caddy.py`, and the cabinet previously lived at `app.jobhuntwow.com`).
Caddy refuses a config with the **same site address in two blocks**. If an apex `jobhuntwow.com {...}`
block already exists, `caddy validate` fails, the deploy restores the backup and exits non-zero -
safe, but it will not go live. Fix one of:

- remove the legacy apex block from the droplet's Caddyfile (preferred: this repo's block becomes the
  only owner of the name), or
- drop `jobhuntwow.com,` from `deploy/caddy/jobhuntwow.caddy` and serve only `www.jobhuntwow.com`.

Either way the change is a **committed file + a re-run**, never an SSH edit.

## Files - what to edit for what

| Want to change... | Edit this, then `python ship_web.py` |
|---|---|
| The app itself (API, cabinet, chat) | `backend/app/**`, `frontend/src/**` |
| The image (deps, build, entrypoint, SPA serving) | `Dockerfile.web` |
| The container (limits, env, volumes, network) | `docker-compose.web.yml` |
| The vhost / reverse proxy / TLS | `deploy/caddy/jobhuntwow.caddy` |
| The pipeline | `.github/workflows/web-deploy.yml` |
| Runtime secrets | the `JHW_ENV_B64` GitHub secret (never a file in git) |

## Security posture (enforced in the files above)

- Non-root `USER jhw` (uid 10001); `no-new-privileges:true`; `cap_drop: [ALL]`.
- No published ports at all - nothing on `0.0.0.0`.
- `mem_limit: 1g`, `cpus: 1.0`; `json-file` logs capped at 3 x 10 MB.
- `HEALTHCHECK` on `/api/health` in the image *and* in compose.
- Secrets only in `/opt/jobhuntwow/.env` (chmod 600) and GitHub Actions secrets. Never `COPY`d into
  a layer, never printed. `.env` is gitignored, and `ship_web.py` aborts if one is ever staged.
- Not enabled (deliberately, and honestly): read-only root filesystem. It was not verified against
  this backend's write paths, and an unverified control is worse than a documented gap.

## Diagnostics (read-only SSH only - never fix anything by hand)

```bash
docker compose -p jobhuntwow -f /opt/jobhuntwow/docker-compose.web.yml ps
docker logs --tail 100 jhw-web
docker inspect jhw-web -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'   # must be ONE
```

To change anything you found: edit the committed file and run `python ship_web.py`.

## Backups

All app state is JSON on the `jobhuntwow_jhw_data` Docker volume:

```bash
docker run --rm -v jobhuntwow_jhw_data:/d -v "$PWD":/b alpine tar czf /b/jhw_data_backup.tgz -C /d .
```
