# JobHub

A small self-hosted web app that turns your phone's OwnTracks location data into a daily log of
stops (job sites) and drives. You review and correct each day on your phone, record the work
performed and parts used, then export a PDF (and JSON) report that another Claude session uses to
update HubSpot.

Single user. Your location data stays on your own server.

## What it does

- Detects stops and drives from OwnTracks Recorder data; every threshold is adjustable.
- Suggests the business at each stop (your own known sites first, then OpenStreetMap, then Google
  only if you enable it) and learns from what you confirm.
- Per stop: business, address, times, drag-to-move pin, **work performed**, **parts used**, status
  (Draft / Confirmed / Personal). Split, merge, delete, or add a stop by hand.
- Saved **Places** (home, office, vendors) pre-fill and pre-mark matching stops.
- **Truck parts and consumables**: a catalog with stock levels, CSV import/export, reorder levels, non-billable
  consumables, a weekly **Count the truck** screen and a vendor-grouped **Shopping list**.
- **Export** (own page, header link): a text-based PDF and a JSON twin for one day or a date range.
- **Alerts** to your phone through Pushover (for example: no location data during work hours).
- A **live status** box: where the phone is now and how long it has been there.
- Tag a stop **Lunch** (left out, by time only) or **Traffic** (a slow-down: its minutes count as drive time). Export is blocked until no stop is left in Draft.
- **Start day / End day** buttons (either can be used alone; any stop can also be made the start or end, and **End at home** adds a basic Home stop), **one-tap confirm** for saved places (and Confirm all known),
  "usual parts" per business, and **Undo** for delete, merge, split and re-detect.
- Daily database **backups**, and a **Debug data** download of a day (raw points, stops, edits) for troubleshooting.
- Works well on an iPhone and can be added to the Home Screen.

## Quick start (Docker)

You need Docker with Compose, and an OwnTracks Recorder reachable from the server.

```bash
git clone https://github.com/ThebumGreg/stop-journal.git jobhub && cd jobhub
cp .env.example .env          # optional: set APP_PASSWORD (see the table below)
docker compose pull           # or: docker compose up -d --build  to build locally
docker compose up -d
```

Open `http://<server>:8000`, go to **Settings → Recorder**, enter your Recorder address and press
**Find on Recorder** to pick your user and device. The database is `./data/stop_journal.db`.

The image is published by GitHub Actions to `ghcr.io/thebumgreg/stop-journal`. The package is
private (it follows the repository), so log in once on the server with a GitHub personal access
token that has the `read:packages` scope:

```bash
echo <TOKEN> | docker login ghcr.io -u <github-username> --password-stdin
```

### Unraid

**With the template (easiest):** in the Unraid terminal run

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/jobhub.xml \
  https://raw.githubusercontent.com/ThebumGreg/stop-journal/main/unraid/jobhub.xml
```

then go to **Docker → Add Container**, choose **JobHub** from the template list, set an
**App password**, and press **Apply**. Then open JobHub, go to **Settings → Recorder**, enter the
Recorder address and pick your user and device with **Find on Recorder**. The template
runs the container as `99:100` and maps `/mnt/user/appdata/jobhub` to `/data`.

The image is pulled from GitHub's registry. If you have not made the package public (GitHub →
your profile → Packages → stop-journal → Package settings → Change visibility), log in once from
the Unraid terminal with a personal access token that has the `read:packages` scope:

```bash
echo <TOKEN> | docker login ghcr.io -u <github-username> --password-stdin
```

**With Compose:** install the **Docker Compose Manager** plugin, create a stack from this repo's
`docker-compose.yml`, put your `.env` beside it and add `PUID=99` and `PGID=100` to it.

To update: use "force update" on the container (or `docker compose pull && docker compose up -d`).

## Configuration

Everything is an environment variable (`.env`). Many of these can also be changed on the
**Settings** page, which overrides the value from `.env`; leaving a field blank there falls back to
`.env`.

| Variable | Default | Notes |
| --- | --- | --- |
| `RECORDER_URL`, `RECORDER_USER`, `RECORDER_DEVICE`, `RECORDER_AUTH` | | optional: normally set in the app (Settings → Recorder → Find on Recorder). Env values are only the starting default |
| `APP_TZ` | `America/Los_Angeles` | everything is shown in this zone, stored as UTC |
| `APP_PASSWORD` | | **set this.** Enables the login page |
| `STOP_MIN_MINUTES`, `STOP_RADIUS_M`, `STOP_MAX_GAP_MIN`, `STOP_MERGE_GAP_MIN`, `STOP_SHORT_MIN_S`, `MAX_ACC_M` | 10, 100, 15, 60, 180, 100 | detection tuning (STOP_SHORT_MIN_S: pauses shorter than this, like traffic lights, are ignored) |
| `PLACES_PROVIDER` | `osm` | `osm`, `osm_google` (OSM first, Google only when OSM comes up short) or `google` |
| `GOOGLE_PLACES_KEY`, `GOOGLE_DAILY_CAP` | | Google is optional and costs money; the cap limits requests per day |
| `OVERPASS_URL` | public server | point at a mirror if lookups fail |
| `PUSHOVER_TOKEN`, `PUSHOVER_USER` | | notifications; see Settings |
| `APP_URL` | | address that notification taps open, e.g. `http://192.168.x.x:8000` |
| `ALERT_NO_DATA`, `ALERT_NO_DATA_MIN`, `WORK_DAYS`, `WORK_START`, `WORK_END`, `ALERT_REPEAT_MIN` | on, 60, Mon-Fri, 07:00, 17:00, 0 | the no-data alert (tapping Start/End day overrides the work hours for that day) |
| `ALERT_DRAFTS`, `ALERT_LOW_STOCK` | on, on | end-of-day "stops still in Draft" reminder, low truck stock alert (generic text only) |
| `ROUTING_URL` | public OSRM server | road distance used only to document miles from where the day ended to Home; coordinates are rounded to ~100 m; blank = off (estimate) |
| `BACKUP_KEEP` | 14 | daily database snapshots to keep (0 = off) |
| `PUID`, `PGID` | 1000 | compose only: the user the container runs as |

## Backup and restore

The SQLite file in `/data` is the entire state (stops, edits, parts, places, settings).

- **Automatic**: once a day JobHub saves a consistent snapshot to `/data/backups/` (`jobhub-YYYY-MM-DD.db`) and
  keeps the newest 14 (Settings → Backups; `BACKUP_KEEP`). Settings also has **Back up now** and download links.
- **Off the server**: these snapshots sit on the same disk, so copy `/data/backups/` elsewhere on a schedule
  (Unraid appdata backup, rsync, cron).
- **Restore**: stop the container, copy a snapshot over `data/stop_journal.db` (renaming it), start it.

## Security and privacy

- Do **not** expose the app to the internet without protection. Safe options: LAN only, a VPN
  (WireGuard/Tailscale), or a reverse proxy with authentication and HTTPS. Always set `APP_PASSWORD`.
- The only outbound calls are place lookups (coordinates and search text go to the provider you
  choose), one road-distance lookup (rounded coordinates, `ROUTING_URL`) when a day ends away from home, map tiles, and Pushover notifications (whose text never contains locations).
- Request URLs contain coordinates, so the server keeps its access log off. Nothing is logged to
  stdout that includes locations. No analytics or telemetry.
- If you ever track technicians, that needs written consent and work-hours-only tracking. This app
  is built for a single owner and does not support it.

## OwnTracks notes

OwnTracks may send nothing while the phone is stationary, depending on its mode. Keep the
no-data alert threshold generous, or switch OwnTracks to a mode that reports regularly.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/pytest -q
SAMPLE_DATA=1 DATA_DIR=./data-sample .venv/bin/flask --app "app:create_app" run --port 5000
```

`SAMPLE_DATA=1` serves synthetic days (2026-11-17 and 2026-11-18) instead of reading the Recorder
and turns the background alert checker off. CI (`.github/workflows/docker.yml`) runs the tests and
publishes the image on every push to `main`.

Stack: Python 3.12, Flask, SQLite, Jinja, Leaflet + OpenStreetMap, ReportLab (PDF).
The bundled DejaVu fonts (`app/fonts`) are under their own free license.
