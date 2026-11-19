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
- **Truck parts**: a catalog with stock levels, CSV import/export, reorder levels.
- **Export**: a text-based PDF and a JSON twin for one day or a date range.
- **Alerts** to your phone through Pushover (for example: no location data during work hours).
- Works well on an iPhone and can be added to the Home Screen.

## Quick start (Docker)

You need Docker with Compose, and an OwnTracks Recorder reachable from the server.

```bash
git clone https://github.com/ThebumGreg/stop-journal.git jobhub && cd jobhub
cp .env.example .env          # then edit .env (see the table below)
docker compose pull           # or: docker compose up -d --build  to build locally
docker compose up -d
```

Open `http://<server>:8000`. The database is `./data/stop_journal.db`.

The image is published by GitHub Actions to `ghcr.io/thebumgreg/stop-journal`. The package is
private (it follows the repository), so log in once on the server with a GitHub personal access
token that has the `read:packages` scope:

```bash
echo <TOKEN> | docker login ghcr.io -u <github-username> --password-stdin
```

### Unraid

1. Install the **Docker Compose Manager** plugin (Community Applications), or use **Docker →
   Add Container**.
2. Log in to GHCR from the Unraid terminal (command above).
3. Compose route: create a stack with this repo's `docker-compose.yml`, put your `.env` beside it
   and set `PUID=99` and `PGID=100` in it. Container route: repository
   `ghcr.io/thebumgreg/stop-journal:latest`, port `8000`, a path mapping
   `/mnt/user/appdata/jobhub` → `/data`, extra parameter `--user 99:100`, and the variables below.
4. To update: `docker compose pull && docker compose up -d` (or "force update" on the container).

## Configuration

Everything is an environment variable (`.env`). Many of these can also be changed on the
**Settings** page, which overrides the value from `.env`; leaving a field blank there falls back to
`.env`.

| Variable | Default | Notes |
| --- | --- | --- |
| `RECORDER_URL` | | e.g. `http://192.168.x.x:8083` |
| `RECORDER_USER`, `RECORDER_DEVICE` | | OwnTracks user and device names on the Recorder |
| `RECORDER_AUTH` | | optional basic auth, `user:pass` |
| `APP_TZ` | `America/Los_Angeles` | everything is shown in this zone, stored as UTC |
| `APP_PASSWORD` | | **set this.** Enables the login page |
| `STOP_MIN_MINUTES`, `STOP_RADIUS_M`, `STOP_MAX_GAP_MIN`, `STOP_MERGE_GAP_MIN`, `MAX_ACC_M` | 10, 100, 15, 60, 100 | detection tuning |
| `PLACES_PROVIDER` | `osm` | `osm`, `osm_google` (OSM first, Google only when OSM comes up short) or `google` |
| `GOOGLE_PLACES_KEY`, `GOOGLE_DAILY_CAP` | | Google is optional and costs money; the cap limits requests per day |
| `OVERPASS_URL` | public server | point at a mirror if lookups fail |
| `PUSHOVER_TOKEN`, `PUSHOVER_USER` | | notifications; see Settings |
| `APP_URL` | | address that notification taps open, e.g. `http://192.168.x.x:8000` |
| `ALERT_NO_DATA`, `ALERT_NO_DATA_MIN`, `WORK_DAYS`, `WORK_START`, `WORK_END`, `ALERT_REPEAT_MIN` | on, 60, Mon-Fri, 07:00, 17:00, 0 | the no-data alert |
| `PUID`, `PGID` | 1000 | compose only: the user the container runs as |

## Backup and restore

The SQLite file in `/data` is the entire state (stops, edits, parts, places, settings).

- **Online backup** (safe while running):
  ```bash
  docker exec jobhub python -c "import sqlite3; s=sqlite3.connect('/data/stop_journal.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"
  ```
  then copy `data/backup.db` somewhere safe. Run it on a schedule (cron or Unraid User Scripts).
- **Restore**: stop the container, replace `data/stop_journal.db`, start it.

## Security and privacy

- Do **not** expose the app to the internet without protection. Safe options: LAN only, a VPN
  (WireGuard/Tailscale), or a reverse proxy with authentication and HTTPS. Always set `APP_PASSWORD`.
- The only outbound calls are place lookups (coordinates and search text go to the provider you
  choose), map tiles, and Pushover notifications (whose text never contains locations).
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
