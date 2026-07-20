# Separating `jobhuntwow-app` into its own project

This folder currently lives inside the Colt cyber pre-sales monorepo (`feranicus/electronic` /
"Linkedin Scraper"). Nothing here depends on that repo — it only shares the droplet + the Caddy vhost.
Below is the clean way to lift it into a standalone repo, **with full git history preserved**.

## Option A — keep history (recommended): `git subtree split`
From the monorepo root:
```bash
# 1) create a branch containing ONLY jobhuntwow-app's history
git subtree split --prefix=jobhuntwow-app -b jobhuntwow-app-only

# 2) make the new repo and push that branch as its main
cd ..
git init jobhuntwow-app && cd jobhuntwow-app
git pull ../<monorepo-folder> jobhuntwow-app-only
git branch -M main
git remote add origin https://github.com/feranicus/jobhuntwow-app.git
git push -u origin main
```

## Option B — fresh start (no history)
```bash
cp -r jobhuntwow-app /somewhere/jobhuntwow-app && cd /somewhere/jobhuntwow-app
git init && git add -A && git commit -m "JobHuntWOW app: initial import"
git remote add origin https://github.com/feranicus/jobhuntwow-app.git
git branch -M main && git push -u origin main
```

## After the split
1. **Remove it from the monorepo** so it isn't maintained in two places:
   ```bash
   git rm -r jobhuntwow-app && git commit -m "Move jobhuntwow-app to its own repo"
   ```
2. **Add `.gitignore`** in the new repo (Node + Python + env):
   ```
   node_modules/
   dist/
   __pycache__/
   *.pyc
   .env
   *.env
   .venv/
   ```
   (Do NOT commit `.env` — only `.env.example`.)
3. **CI (optional):** a GitHub Actions workflow that builds the images and deploys over SSH/Tailscale to
   the droplet, then `docker compose up -d`. Mirror the pattern already used for cybergod.ai if you want
   push-to-deploy.
4. **Move the droplet deploy scripts too.** `deploy_jobhuntwow_caddy.py` and `deploy_jobhuntwow_obs.py`
   currently sit in the Colt monorepo ROOT but belong to JobHuntWOW — they deploy the `.com` static
   one-pager onto the droplet's Caddy and wire its Grafana observability. Move them into the jobhuntwow
   project (e.g. a `deploy/` folder) so nothing jobhuntwow-related stays in the Colt repo.
5. **Docs shipped with this project:** `README.md`, `CLAUDE.md`, `ARCHITECTURE.md`, `DEPLOY.md`, this file.

## Relationship to `jobhuntwow.com`
`jobhuntwow.com` is already its own repo (`github.com/feranicus/jobhuntwow.com`): the GitHub-Pages
landing + the Google-Apps-Script Gmail→Sheets tracker. Two clean options:
- **Two repos** (recommended): `jobhuntwow.com` (site + tracker) and `jobhuntwow-app` (cabinet). This
  doc assumes that.
- **One org, monorepo**: a `jobhuntwow` org repo with `/site` and `/app` — only if you want a single CI.

Keep secrets out of both, keep the `/api/*` contract stable, and the two halves stay independently shippable.
