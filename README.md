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
- Per stop: first pick **what the stop is** (Job, Vendor, Office, Gas, Lunch, Personal, Traffic); the card then shows only
  what that type needs, and fields save on their own. Jobs, vendors and office stops have one **Confirm** button that says
  what is still missing. Job and Vendor stops can pick from your saved clients or vendors; choosing Office (or Personal at
  Home) fills in the saved place. Business, address, times, drag-to-move pin, **work performed**, **parts used**, **photos and
  receipts**. Split, merge, delete, or add a stop by hand.
- **Gas** stops log gallons, total and odometer (the time at the pump counts as drive time); the Truck page's **Fuel**
  panel shows spend, gallons, average price, MPG and fuel cost per mile.
- Saved **Places** (home, office, vendors) pre-fill and pre-mark matching stops.
- **Truck** has tabs: Parts (each part has its own page with stock, shelf and bin, where it was used, what you paid and every
  stock change), Stock (count shelf by shelf, shopping list with Bought and Share, purchases), Vendors (one per vendor across
  branches), Kits (a set of parts added to a stop in one tap), Tools (what you own: purchase date, price, vendor, serial,
  warranty, receipt and photo), Fuel and Import / export (parts and tools CSV).
- **Truck parts and consumables**: a catalog with stock levels, fixed unit list and pack sizes (buy a bag of 25, use them one by one), vendor and category dropdowns, CSV import/export, reorder levels, non-billable
  consumables, a weekly **Count the truck** screen and a vendor-grouped **Shopping list**.
- **Reports** (own page, header link): a text-based PDF and a JSON twin for one day or a date range. **Include** ticks leave
  out sections (part costs, expenses, fuel, receipts and photos, materials, hours by category, follow-ups, mileage, recap);
  the report says what was left out. **Zip: PDF + photos** downloads the PDF, the JSON and every receipt and photo in the
  report, filed as `photos/YYYY/MM/DD/stop-1-acme-receipt-1.jpg` (the PDF prints each picture's file name).
  From the Home Screen app the files open the share sheet (Save to Files, Mail).
- **Home page**: where you are, the next on-site appointment with drive time ("leave by", or an ETA while driving), and
  what is still unfinished today. The day view is under **Day**. **Go to a place** opens Apple or Google Maps to a saved
  place and fills in the stop when you get there.
- **Running late** (optional, Pushover): when the drive time says you will be late for an on-site appointment you get an
  alert (no names in it); the Home page then offers **Tell the customer**, which emails your work AI agent a late notice.
  Only on that tap.
- **Send for invoice**: a finished Time & Material stop can be emailed to your work AI agent right away (time on site,
  billable parts and expenses, after a preview) so it can invoice while you are still there. Only on that tap.
- **Vendor stops record what you bought** ("Bought here"): it goes into truck stock and the purchase log, never billed.
  **Truck → Receipts** turns photos of old gas and store receipts into stops (for days with no GPS).
- Parts that were never used on a stop can be **deleted permanently**; used ones are removed from the catalog and keep
  their history.
- **AI notes** (optional, Anthropic API key in Settings → AI): ✨ Polish notes fixes spelling and grammar in Work performed
  and shows the result for you to accept; only that text is sent.
- **Receipt reading** (optional: Google Cloud Vision, or Claude for store, date, tax and line items): a receipt photo suggests
  the fuel total and gallons, a store total as an expense, items to add as parts, or a tool's price, date and store; you tap to
  use it.
- **Places history**: tap a client, vendor, office or gas station to see every visit (date, time, work, parts, fill-ups) and
  tap a visit to open it.
- **Outlook calendar** (optional): paste the published ICS link in Settings → My calendar; appointments with "onsite" in them show
  on the day, next to the stop they match, with a one-tap "Use calendar" to fill in the name and address.
- **Alerts** to your phone as native iPhone notifications (Settings → Account → Notifications, on the https address)
  and/or through Pushover (for example: no location data during work hours, or you drove away from a stop
  you had not finished; tapping that one opens the stop).
- A **live status** box: where the phone is now and how long it has been there.
- Sync, Confirm all known and Undo are on the **Reports** page. Once the day is ended, no more stops are added to it.
- Tag a stop **Lunch** (left out, by time only) or **Traffic** (a slow-down: its minutes count as drive time). Export is blocked until no stop is left in Draft.
- **Start day / End day** buttons (either can be used alone; any stop can also be made the start or end, and **End at home** adds a basic Home stop), **one-tap confirm** for saved places (and Confirm all known),
  "usual parts" per business, and **Undo** for delete, merge, split and re-detect.
- A **Notes & parts** sheet per stop slides up for the work performed, category, job/ticket reference, parts, expenses (parking,
  tolls) and follow-ups, with autosave and a Saved / Unsaved line. The report adds hours by category, expenses and follow-ups, and
  the Export page warns about missing categories or notes (it never blocks).
- Daily database **backups**, and a **Debug data** download of a day (raw points, stops, edits) for troubleshooting.
- Works well on an iPhone and can be added to the Home Screen.

## Quick start (Docker)

You need Docker with Compose, and an OwnTracks Recorder reachable from the server.

```bash
git clone https://github.com/ThebumGreg/stop-journal.git jobhub && cd jobhub
cp .env.example .env          # then open JobHub on your network and create your login (Settings → Account)
docker compose pull           # or: docker compose up -d --build  to build locally
docker compose up -d
```

Open `http://<server>:8000`, go to **Settings → OwnTracks Recorder**, enter your Recorder address, then in **My phone (OwnTracks)** press
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
**App password**, and press **Apply**. Then open JobHub, go to **Settings → OwnTracks Recorder**, enter the
Recorder address, then pick your user and device in **My phone (OwnTracks)** with **Find on Recorder**. The template
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
| `RECORDER_URL`, `RECORDER_USER`, `RECORDER_DEVICE`, `RECORDER_AUTH` | | optional: normally set in the app (Settings → My phone (OwnTracks) → Find on Recorder). Env values are only the starting default |
| `APP_TZ` | `America/Los_Angeles` | everything is shown in this zone, stored as UTC |
| `APP_PASSWORD` | | only until you create your login (Settings → Account); ignored after that |
| `STOP_MIN_MINUTES`, `STOP_RADIUS_M`, `STOP_MAX_GAP_MIN`, `STOP_MERGE_GAP_MIN`, `STOP_SHORT_MIN_S`, `MAX_ACC_M` | 10, 100, 15, 60, 180, 100 | detection tuning (STOP_SHORT_MIN_S: pauses shorter than this, like traffic lights, are ignored) |
| `PLACES_PROVIDER` | `osm` | `osm`, `osm_google` (OSM first, Google only when OSM comes up short) or `google` |
| `GOOGLE_PLACES_KEY`, `GOOGLE_DAILY_CAP` | | Google is optional and costs money; the cap limits requests per day |
| `OVERPASS_URL` | public server | point at a mirror if lookups fail |
| `PUSHOVER_TOKEN`, `PUSHOVER_USER` | | Pushover notifications; see Settings |
| `NOTIFY_CHANNEL` | `both` | where alerts go: `both`, `iphone` (native notifications) or `pushover`; each only when set up |
| `APP_URL` | | JobHub's public https address (notification links, password reset emails) |
| `ALERT_NO_DATA`, `ALERT_NO_DATA_MIN`, `WORK_DAYS`, `WORK_START`, `WORK_END`, `ALERT_REPEAT_MIN` | on, 60, Mon-Fri, 07:00, 17:00, 0 | the no-data alert (tapping Start/End day overrides the work hours for that day) |
| `ALERT_DRAFTS`, `ALERT_LOW_STOCK`, `ALERT_LEFT_STOP` | on, on, on | end-of-day "stops still in Draft" reminder, low truck stock alert, "you left a stop that isn't finished" (generic text only) |
| `ALERT_LATE`, `ALERT_LATE_MIN` | on, 10 | "running late" alert when the drive time says you will reach an on-site appointment this many minutes after it starts |
| `MAIL_TO`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM`, `SMTP_HOST`, `SMTP_PORT` | | Gmail for reports, invites, late notices and invoice requests (see Email reports) |
| `LATE_NOTICE_TO` | blank (= `MAIL_TO`) | where **Tell the customer** sends late notices (your agent's address) |
| `MESSAGE_FORMAT` | `1` | JSON layout of every email to the agent: `1`, or `2` (one envelope and shared objects; switch after the agent checked a test day) |
| `REPORT_SIGNING_KEY` | | optional shared phrase; emails then carry an HMAC signature of the attached JSON |
| `CALENDAR_INVITES`, `CALENDAR_INVITE_MIN` | off, 10 | put today's stops on your Outlook calendar as invites |
| `CALENDAR_URL`, `CALENDAR_KEYWORD` | , onsite | published Outlook (ICS) link and the word that marks on-site appointments |
| `TECH_NAME`, `TECH_EMAIL`, `FOLLOWUP_OWNERS`, `LUNCH_PAID` | , , , on | technician on reports, follow-up owners (`Name = email`, `|`-separated), whether lunch is paid |
| `ANTHROPIC_API_KEY`, `AI_NOTES`, `RECEIPT_READER` | , on, off | Claude for Polish notes and reading receipts (`claude`, or `google_vision`) |
| `QBO_IMPORT_SERVICE_ITEMS_WITH_SKU` | on | QuickBooks parts import: Service items with a SKU count as parts |
| `LOG_LEVEL` | info | `debug` adds page steps to the log (Settings → Log and backups) |
| `AUTO_SYNC_MIN` | `2` | while today's day page is open, pick up new points and stops every this many minutes (0 = off); also a Settings value |
| `LANGUAGETOOL_URL` | blank (off) | your own LanguageTool server, e.g. `http://192.168.1.10:8010`; adds a **Check grammar** button to the notes sheet. Only the note text is sent, to that address. Also a Settings value |
| `ROUTING_URL` | public OSRM server | road distance used only to document miles from where the day ended to Home; coordinates are rounded to ~100 m; blank = off (estimate) |
| `BACKUP_KEEP` | 7 | daily database snapshots to keep (0 = off) |
| `STATUS_TOKEN` | blank (off) | secret bearer token for the status API (see Status API); set it only as an environment variable |
| `PUID`, `PGID` | 1000 | compose only: the user the container runs as |

## Status API (dashboard and uptime monitor)

Two read-only addresses, for a [Homepage](https://gethomepage.dev) widget and Uptime Kuma. They work without the
JobHub sign-in, use no cookie, and hold no names, places or addresses. GET only (anything else answers 405).

| Address | Sign-in | Answer |
| --- | --- | --- |
| `/healthz` | none | `200 {"status":"ok"}`, or `503 {"status":"down"}` when the database cannot be read |
| `/api/status` | `Authorization: Bearer <STATUS_TOKEN>` | `200` (ok or degraded) or `503` (down) with the JSON below; `401` for a wrong token; `404` while `STATUS_TOKEN` is unset |

Make a token with `openssl rand -hex 32` and set it as `STATUS_TOKEN` (`.env`, or a variable on the Unraid
container). Never commit it.

```json
{
  "app": "jobhub",
  "version": "2.0.0",
  "status": "ok",
  "checked_at": "2026-11-27T16:51:00Z",
  "uptime_seconds": 86400,
  "checks": {
    "database": {"status": "ok", "latency_ms": 3},
    "recorder": {"status": "ok", "latency_ms": 41}
  },
  "metrics": {
    "stops_today": 6,
    "unfinished_stops": 2,
    "reports_pending": 0,
    "last_point_at": "2026-11-27T16:49:10Z",
    "last_stop_at": "2026-11-27T16:40:12Z",
    "last_backup_at": "2026-11-27T08:00:03Z"
  }
}
```

- `status` is the worst of `checks`. `database` down means `down`. The `recorder` check is there only when OwnTracks
  is set up; a failing Recorder means `degraded` (JobHub still shows stored days).
- `stops_today`: stops recorded for today. JobHub adds them when the day page is open, or in the background during
  work hours with the "stop not finished" alert on.
- `unfinished_stops`: stops still in Draft, today and the 14 days before.
- `reports_pending`: past days (14) with confirmed stops that no successful Email report covers; `null` while email is
  not set up.
- `last_point_at`: newest OwnTracks point (the Recorder's, else the newest stored). `last_stop_at`: newest arrival.
  `last_backup_at`: newest database snapshot. Times are UTC; `null` means unknown.
- The result is cached for 15 seconds and each check gives up after 2 seconds. Requests are logged only with Log
  detail = debug.

Homepage (`services.yaml`; put the token in `HOMEPAGE_VAR_JOBHUB_STATUS_TOKEN` on the Homepage container):

```yaml
- JobHub:
    icon: mdi-briefcase-outline
    href: https://jobhub.example.com
    siteMonitor: http://HOST:8000/healthz
    widget:
      type: customapi
      url: http://HOST:8000/api/status
      refreshInterval: 30000
      headers:
        Authorization: "Bearer {{HOMEPAGE_VAR_JOBHUB_STATUS_TOKEN}}"
      mappings:
        - field: status
          label: Status
          format: text
        - field: metrics.stops_today
          label: Stops today
          format: number
        - field: metrics.unfinished_stops
          label: To finish
          format: number
        - field: metrics.last_point_at
          label: Last location
          format: relativeDate
```

Uptime Kuma:

1. **HTTP(s)** monitor on `http://HOST:8000/healthz`, accepted status 200, every 60 s.
2. Optional **HTTP(s) - Json Query** monitor on `http://HOST:8000/api/status` with the header
   `{"Authorization": "Bearer <token>"}`, query `status`, expected value `ok` (it also alerts on `degraded`).

## Email reports (Gmail)

For a work AI agent that updates HubSpot from your mailbox: Reports page → **Email report** sends the JSON (every field,
schema version 1, or 2 per Settings → Work AI agent → Message format) and the PDF, optionally the receipts and photos, to your work address. Subject
`JobHub Daily Summary 2026-11-23`; sending the same dates again adds `(update 2)` and the body says it replaces the
earlier one. The body starts with `Dates:`, `Version:`, `Confirmed stops:` and `Attached:` lines and a short stop list.

Setup: turn on 2-Step Verification on the Gmail account, then Google Account → Security → App passwords → create one
named JobHub. In Settings → Work AI agent fill in "Send reports to"; in Settings → Email the Gmail address and the 16-letter App password, then
**Send test email**. Env equivalents: `MAIL_TO`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM`, `SMTP_HOST`
(smtp.gmail.com), `SMTP_PORT` (587).

### Stops on your Outlook calendar

Settings → My calendar → "Put my stops on my work calendar": every stop today is sent as a calendar invite to "Send reports to"
(type, customer name, address, ticket, times, finished; never notes or prices), updated every 10 minutes when it changes and
cancelled if the stop is deleted. Use "Send test calendar invite" first. Coworkers see the events if they can see your calendar.

## Report fields (for the work AI agent)

The JSON report (schema version "1"; new fields are only ever added) is what the agent should read. A full made-up example
and a guide for the agent are in `docs/sample-report/` (regenerate with `python scripts/make_sample_report.py`). Key points:
- `technician` {name, email}; each stop has a stable `stop_uid` (use it, not `stop_number`, to update earlier records),
  `hubspot_company_id` (from the saved place: Places → Edit place, or the places CSV), `hubspot_ticket` and `qbo_estimate`.
- Follow-ups have `owner` and `owner_email` (Settings → Me → Follow-up owners) and a `follow_up_id`.
- `summary.paid_minutes` = on-site + drive + lunch (when `lunch_paid`; Settings → Pay rules). `drive_minutes`
  includes `traffic_minutes` (time at fuel stops and short breaks: `fuel_stop_minutes`, `short_break_minutes`, plus traffic).
  The `timesheet` rows marked paid add up to `paid_minutes`.
- Vendor stops list `bought_here` (bought for truck stock, never billed). Stops already sent for invoice carry
  `invoice_request` {version, sent_at, billed_through, time_on_site_minutes}: the agent must not invoice them again.
- Two more emails go to the agent, only when you tap: `JobHub Invoice Request <date> – <customer>` and
  `JobHub Late Notice <ticket>`. Their formats and samples are in `docs/sample-report/README.md`.
- **Message format 2** (Settings → Work AI agent → Message format, `MESSAGE_FORMAT`): the agent's newer layout for all three
  emails: one envelope (`message_id`, `revision`, `supersedes`), the same Stop object in the daily summary and the
  invoice request, ISO times with seconds, money as text, UUIDs on every line, follow-up and photo, the address in parts
  (Places → Edit place: street, suite, city, state, ZIP). Samples in `docs/sample-report/v2/`. Send one test day first
  (Email report → "Send as format 2 (test)").

## Backup and restore

The SQLite file in `/data` holds the state (stops, edits, parts, places, settings). Photos and receipts are files next to
it in `/data/photos/YYYY/MM/DD/` (by the stop's day, shrunk to 1600 px, location metadata removed); the database snapshots
do not contain them, so back up the whole `/data` folder (at least `backups/` and `photos/`).

- **Automatic**: once a day JobHub saves a consistent snapshot to `/data/backups/` (`jobhub-YYYY-MM-DD.db`) and
  keeps the newest 7 (Settings → Backups; `BACKUP_KEEP`). Settings also has **Back up now** and download links.
- **Database and photos together**: Settings → Backups → **Download everything (database + photos)** gives one zip. Keep it off the server.
- **Off the server**: these snapshots sit on the same disk, so copy `/data/backups/` elsewhere on a schedule
  (Unraid appdata backup, rsync, cron).
- **Restore**: stop the container, copy a snapshot over `data/stop_journal.db` (renaming it), start it.

## Logs

JobHub writes one line per request (path only), errors with a short code (the page shows the same code), and events
such as syncs, emails, alerts, invites and backups. Coordinates are never written. Lines starting with `sent` are the
server's delivery log (path only, never the query string): a request with no `sent` line never reached the phone.
Once a week old working data is trimmed (calendar-invite log, map and route caches, old one-off alert markers, old
trips); stops, parts, purchases, photos and email records are never trimmed. Settings → Log and backups: detail
(info / debug), **View recent log** and **Download log**.

On Unraid: Docker tab → JobHub icon → **Logs**, or `docker logs --tail 200 JobHub` (`docker logs -f JobHub` to follow).
`docker compose` commands only work inside the folder that holds `docker-compose.yml`. Turn on log rotation in
Settings → Docker (Docker log rotation) so the container log cannot fill the disk.

## Security and privacy

- Do **not** expose the app to the internet without protection. Safe options: LAN only, a VPN
  (WireGuard/Tailscale), or a Cloudflare tunnel with Cloudflare Access in front (the setup the owner uses; see
  `docs/SETUP-CHECKLIST.md` section 10).
- **Sign-in:** a username and password (Settings → Account), stored only as a salted scrypt hash. You stay signed in
  for 30 days after your last visit. Changing the password, or **Sign out everywhere else**, signs out every other
  device. Ten wrong tries from one address pause sign-in from it for 15 minutes; thirty for one username pause that
  username for outside addresses (the home network can still sign in); a pause sends one Pushover note with no names.
  **Forgot password?** emails a one-time link, valid 30 minutes, to the account's email (through the Gmail account in
  Settings → Email); the page never says whether an account exists. From the server console:
  `docker exec -it JobHub python -m app.auth reset <username>` (also `list`, `create`).
- At home the same https address can go through your own reverse proxy (split DNS: the local DNS answers with the
  proxy's address). Don't forward a router port to it: that way in skips Cloudflare Access. Set the proxy not to cache
  JobHub's files. Details: `docs/SETUP-CHECKLIST.md` section 10.
- Expose only `/pub` of the OwnTracks Recorder to the internet, behind a password.
- Nothing behind the sign-in is cached by browsers or proxies (photos only on your own device). Coordinates, search
  text and names are never put in a web address, so a proxy's access log doesn't collect them.
- Backups and the data folder hold your Gmail app password, API keys and sign-in key: keep them private.
- Until a login exists, anything arriving through the Cloudflare tunnel is refused. Changes (POST) coming from another
  website are refused. The sign-in cookie is Secure over https, HttpOnly and SameSite=Lax; pages can't be framed by
  other sites. Accounts are a table, so more logins can be added later (only one is offered today).
- Safari download links ("Copy link for Safari", 10 minutes) work for reports, exports and the log only, never for
  backups.
- The only outbound calls are place lookups (coordinates and search text go to the provider you
  choose), grammar checks (only if you set `LANGUAGETOOL_URL`, sent to your own server), one road-distance lookup (rounded coordinates, `ROUTING_URL`) when a day ends away from home, reading your published calendar (only if you set the link), Google Vision for receipts and Claude for Polish notes (only if
  turned on; receipt photos and the note text respectively), map tiles, and notifications: Pushover and/or native ones through the push service of each phone or browser you turned
  on (Apple, Google, Mozilla or Microsoft; JobHub accepts no other address). Their text never contains locations,
  customers or tickets.
  With the running-late alert on, JobHub also looks up the drive time to your next appointment every few minutes during
  the work day (rounded coordinates to the routing server; the appointment's address to OpenStreetMap search, cached).
- **Email report** (Settings → Email) sends the report, including customer names, addresses, notes and (unless you
  untick "GPS coordinates") coordinates, to the address you set, through your Gmail account. It is sent only when you
  tap Email report on the Reports page. Calendar invites (when turned on) send each stop's type, customer, address,
  ticket and times, never notes or prices. **Send for invoice** and **Tell the customer** email one stop or appointment
  (customer, address, ticket, work, parts) to your agent, only when you tap them.
- Request URLs can contain coordinates, so every log line (the app's and the server's delivery log) has the path only,
  never the query string, and anything that looks like a coordinate is blanked. No analytics or telemetry.
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

How each feature works (time rules, mileage, reports, parts, alerts, emails) is written up in `docs/FEATURES.md`;
`CLAUDE.md` has the project rules, a code map and the plan. After changing the report or the emails, run
`python scripts/make_sample_report.py` to refresh `docs/sample-report/`.

Stack: Python 3.12, Flask, SQLite, Jinja, Leaflet + OpenStreetMap, ReportLab (PDF).
The bundled DejaVu fonts (`app/fonts`) are under their own free license.
