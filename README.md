# Mallorca 70.3 dashboard — tri.jonmercado.com

## Files
- `index.html` — the live dashboard page (this gets overwritten by each refresh; the copy in this zip is a real snapshot so the site isn't blank on first deploy)
- `dashboard_template.html` — the template `refresh_dashboard.py` renders into `index.html`, with `__DATA_JSON__` as the data placeholder
- `refresh_dashboard.py` — pulls fresh data and writes the rendered HTML
- `requirements.txt` — `garminconnect`, `requests`
- `run_refresh.sh` — cron wrapper (throwaway `python:3.11-slim` container, same pattern as your Spotify tool), commits + pushes the regenerated file so Cloudflare Pages redeploys
- `functions/_middleware.js` — Cloudflare Pages Function that password-gates the whole site with plain HTTP Basic Auth (browser's native prompt — no custom login page, no cookies)

## What changed vs. the one-off version
TrainingPeaks has no public API for an individual account, so CTL/ATL/TSB
are now **modeled locally** from Strava's Relative Effort per activity,
using the same 42-day/7-day exponentially-weighted formula TrainingPeaks
and intervals.icu both use. Numbers will track closely but won't match
TrainingPeaks to the decimal. Everything else (sleep, HRV, readiness,
resting HR, body battery, VO2max, weekly hours, load balance/ACWR,
pace trends, aerobic efficiency, calories) comes straight from Garmin/Strava.

## GitHub setup

1. Finish creating the `kasarap/tri` repo on GitHub (Public is fine — no
   secrets ever live in this repo; passwords and tokens stay in Cloudflare's
   env vars and Unraid's `secrets.env`, never committed).
2. Unzip this file's contents into a local clone and push:
   ```bash
   git clone https://github.com/kasarap/tri.git
   cd tri
   # copy in everything from this zip
   git add .
   git commit -m "Initial dashboard, refresh script, password gate"
   git push
   ```

## Cloudflare Pages setup

1. Cloudflare dashboard → **Workers & Pages** → **Create** → **Pages** →
   **Connect to Git** → select `kasarap/tri`.
2. Framework preset: **None**. Build command: *(leave empty)*. Build
   output directory: `/`. Deploy.
3. **Custom domain**: on the new Pages project → **Custom domains** →
   **Set up a custom domain** → `tri.jonmercado.com`. Since
   `jonmercado.com` is already your Cloudflare zone, the CNAME is added
   automatically — no manual DNS edit, same as `nf.jonmercado.com`.
4. **Password**: Pages project → **Settings** → **Environment variables**
   → **Add variable** → name `SITE_PASSWORD`, value whatever you want,
   check **Encrypt** → add for both **Production** and **Preview**.
5. Trigger a redeploy (env var changes need a fresh deploy — push any
   commit, or redeploy manually from the **Deployments** tab).
6. Visit `tri.jonmercado.com` — your browser should show its native
   password prompt (type anything for username, the real password in
   the password field). It'll offer to remember it, mobile included.

## Unraid refresh automation setup

**1. Strava** — create an app at strava.com/settings/api if you don't
already have one, then get a refresh token with the `activity:read_all`
scope (standard OAuth authorize-code exchange, same shape as your
Spotify token setup). You need `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`,
`STRAVA_REFRESH_TOKEN`.

**2. Garmin** — the `garminconnect` library caches its session token to
disk after one successful login. Easiest path: reuse the login flow you
already have for `garmin-connect-mcp` (Desktop `garmin-connect-mcp`
folder, prompts MFA, writes to `~/.garminconnect`), then copy that
token folder to `/mnt/user/appdata/mallorca-dashboard/garmin-token-cache/`
on Unraid. After that, `run_refresh.sh` doesn't need your Garmin
password at all — only set `GARMIN_EMAIL`/`GARMIN_PASSWORD` in
`secrets.env` as a fallback for when the token eventually expires.

**3. On Unraid**, create `/mnt/user/appdata/mallorca-dashboard/` with:
- `refresh_dashboard.py`, `dashboard_template.html`, `requirements.txt` (copy in)
- `garmin-token-cache/` (seeded per step 2)
- `secrets.env` (chmod 600) —
  ```
  STRAVA_CLIENT_ID=...
  STRAVA_CLIENT_SECRET=...
  STRAVA_REFRESH_TOKEN=...
  ```
- `site-repo/` — a real `git clone` of `kasarap/tri`, with a push-capable
  remote (SSH deploy key, same approach you used for `browser-bridge-mcp`)

Edit `REPO_DIR` at the top of `run_refresh.sh` to point at that clone —
`OUTPUT_FILENAME` is already `index.html`, matching this repo.

**4. Test it manually** before trusting cron:
```
bash run_refresh.sh
tail -30 /mnt/user/appdata/mallorca-dashboard/run.log
```

**5. Cron** (matches your existing pattern — pick a time, e.g. 5am daily):
```
0 5 * * * bash /mnt/user/appdata/mallorca-dashboard/run_refresh.sh
```
Persist it in `/boot/config/go` too, since Unraid wipes crontab on reboot.

## Alternative: skip Unraid entirely with GitHub Actions
Since the output is a GitHub-hosted static file anyway, you could instead
run `refresh_dashboard.py` on a schedule via a GitHub Actions workflow
(`schedule: cron:`) with the same secrets stored as repo secrets, and have
the workflow commit the regenerated file itself. No home server needed
for this one thing — worth considering if you'd rather not add another
Unraid cron job. Say the word and I'll write that workflow file instead.

## Mobile
The template already has phone-specific CSS (tighter spacing, 2-column
stat grid, shorter charts under 480px, safe-area padding for notches).
Nothing extra needed to view it well on a phone once it's live at your
domain.

