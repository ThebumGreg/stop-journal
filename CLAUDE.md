# Stop Journal: personal job-site and travel logger

A small self-hosted web app that turns OwnTracks location data into a daily log of
stops (job sites) and drives. The user reviews and corrects each day, then exports a
PDF report. The PDF is later given to a separate Claude session that updates HubSpot
records. Direct HubSpot integration comes later, not in v1.

## Scope for v1
- Single user (the owner). No logins beyond a simple shared password or reverse-proxy auth.
- Runs in Docker. Develop in WSL (Ubuntu), deploy to a home server later.
- Read data from an existing OwnTracks Recorder over its HTTP API.
- Detect stops and drives, suggest businesses for each stop, let the user fix everything.
- Export a daily PDF report (and a JSON twin of it).
- NOT in v1: HubSpot API calls, QuickBooks, Auris payroll, multi-user, mobile app.

## Stack (defaults, change if there is a good reason)
- Python 3.12, Flask, SQLite (single file under /data), Jinja templates, htmx or plain JS.
- Map: Leaflet with OpenStreetMap tiles.
- PDF: WeasyPrint (HTML to PDF, real selectable text) or ReportLab.
- Docker + docker-compose. Config entirely through environment variables.
- Prefer free and open-source dependencies. No paid services required to run.

## Configuration (env vars)
- RECORDER_URL (for example http://192.168.x.x:8083), RECORDER_USER, RECORDER_DEVICE
- RECORDER_AUTH (optional basic auth, "user:pass")
- APP_TZ (default America/Los_Angeles); all displayed times are local, stored as UTC
- PLACES_PROVIDER = osm (default) or google; GOOGLE_PLACES_KEY only needed for google
- STOP_MIN_MINUTES (default 10), STOP_RADIUS_M (default 100), STOP_MAX_GAP_MIN (default 15)
- APP_PASSWORD (optional)

## Data source: OwnTracks Recorder
- Locations endpoint: `GET {RECORDER_URL}/api/0/locations?user=U&device=D&from=ISO&to=ISO&format=json`
- Verify the exact response shape against the live Recorder before coding the parser.
  Points carry lat, lon, tst (epoch seconds), acc (accuracy m), vel, batt and similar.
- Cache raw points per day in SQLite so a day can be reprocessed without refetching.
- Drop points with poor accuracy (default acc > 100 m) before clustering.

## Stop and drive detection
1. Fetch the day's points (local midnight to midnight), sort by time, filter by accuracy.
2. Stop = consecutive points that stay within STOP_RADIUS_M of a running centroid for at
   least STOP_MIN_MINUTES. Tolerate gaps up to STOP_MAX_GAP_MIN (phone sleeping, no signal).
3. Merge adjacent stops that are close in space and separated by a short gap.
4. Everything between two stops is a drive: departure time, arrival time, duration,
   distance (sum of haversine between points; label it approximate).
5. All thresholds live in config, and the UI can re-run detection with different values.
6. Write unit tests with fixture days: one normal day, one with GPS drift at a site, one with
   a gap (phone off), one with a quick stop that must NOT count (for example a gas station
   under 10 minutes, still shown as a dismissible "short stop").

## Business suggestions per stop
For each stop, show a ranked pick-list and a free-text search box.
- Provider interface: `search_nearby(lat, lon, radius_m) -> list[Candidate]` and
  `search_text(query, lat, lon) -> list[Candidate]`.
- osm provider: Overpass for nearby POIs with a name (shop, office, amenity, craft,
  industrial, healthcare and similar), Nominatim for text search. Respect their usage
  policies: set a User-Agent, rate limit, cache every result.
- google provider: Places Nearby/Text Search. Optional, only if a key is configured.
- Ranking score (document it in code, keep it tunable):
  1. Candidates from the user's own "known sites" table rank first. This table is
     filled automatically from every business the user has confirmed before, and later
     from HubSpot companies.
  2. Then by distance from the stop centroid (closer is better).
  3. Then by how "business-like" the candidate is (named business over a bare building or
     address), and by dwell time versus typical visit length for that category.
- The user can pick a candidate, type a custom name and address, or mark the stop Personal
  (excluded from the report totals and from the PDF's job list).
- Remember choices: confirming a business at a location teaches the known-sites table
  (name, address, lat/lon, radius), so the same place is one tap next time.

## Editing UI (the daily review screen)
- Date picker with prev/next day. Default to today.
- Left: timeline list of stops and drives in order. Right: map with the day's path and stop
  markers; clicking one syncs both.
- Per stop: business pick-list plus search box, editable address, location nudge (drag the
  pin), editable arrival and departure times, work performed (multi-line; stored in the `notes` column), status: Draft, Confirmed,
  or Personal.
- Split a stop at a time, merge two stops, delete a stop, add a manual stop with no GPS data.
- Drives show time and approximate miles and are editable (time, notes).
- Day footer: total on-site time, total drive time, approximate miles, stops confirmed
  versus still in Draft.
- Edits never change the raw points; they are stored as overrides on top of detected data.
- Keep it mobile-friendly. The user will mostly review on a phone.

## Data model (SQLite)
- points(id, device, tst_utc, lat, lon, acc, raw_json)
- days(date, processed_at, params_json)
- stops(id, date, seq, lat, lon, arrive_utc, depart_utc, business_name, address,
  known_site_id, hubspot_company_id NULL, notes, status, source)
- drives(id, date, seq, from_stop_id, to_stop_id, depart_utc, arrive_utc, distance_m, notes)
- known_sites(id, name, address, lat, lon, radius_m, category, hubspot_company_id NULL,
  times_confirmed, last_confirmed)
- Keep `hubspot_company_id` columns now (nullable) so the later integration needs no migration.

## PDF report (the handoff to the work Claude session)
The PDF is read by another Claude session that will update HubSpot records, so it must be
clean, text-based and unambiguous. No screenshots of maps as the only source of information.
- Export from the UI for one day (and optionally a date range). Also offer the same data as JSON.
- Use real selectable text, simple tables, consistent field labels, 24-hour or clearly
  AM/PM times with the timezone stated once in the header.
- Layout:
  1. Header: report title, date, timezone, generated-at time, report schema version "1".
  2. Day summary: totals for on-site time, drive time, approximate miles, stops confirmed.
  3. One block per CONFIRMED stop, in order, with these labeled fields:
     Stop number, Business name, Address, Latitude/Longitude, Arrival, Departure,
     Time on site, Drive from previous stop (minutes and approximate miles),
     HubSpot company ID (blank for now), Work performed (full text).
  4. A final "Records to update" table, one row per confirmed stop: business name, address,
     arrival, departure, work performed. This is the section the HubSpot session acts on.
  5. A short "Excluded" section listing Personal and unconfirmed stops by time only (no
     detail), so the reader knows they were intentionally left out.
- Only Confirmed stops appear in detail. Draft stops are flagged in the UI before export.
- Add a small text instruction block at the end that states what each section means, so
  the reading session does not have to guess.
- Test by extracting the PDF text and checking every field survives (for example with
  pdftotext) and that it reads in a sensible order.

## Privacy and security
- Location data stays on the user's server. The only outbound calls are place lookups,
  which send coordinates (and search text) to the chosen provider, and tile requests.
- Pushover notifications (alerts, e.g. "no location data") are the one other outbound call. Their
  text must never contain coordinates, addresses or business names: it passes through a third party.
- Never log raw coordinates to stdout in production; no analytics or telemetry.
- Do not expose the app to the internet without auth. Document the safe options: LAN only,
  VPN, or a reverse proxy with auth.
- If technicians are ever added later, that needs written consent and work-hours-only
  tracking; flag this in the README, do not build it now.

## Later (design for, do not build yet)
- HubSpot integration: private app token, match known_sites to HubSpot companies, create
  notes/activities on the company record with times, drive time and notes.
- Cross-check against Auris clock-in/out and QuickBooks time entries; flag mismatches.
- Optional daily recap posted to HubSpot.

## Milestones
1. Project skeleton, Docker, config, Recorder client, raw point cache. Verify against the live Recorder.
2. Stop and drive detection with tests; simple read-only day view on a map.
3. Business suggestions (osm provider), known-sites learning, free-text search.
4. Editing UI (times, pins, notes, split/merge, personal), day totals.
5. PDF + JSON export, with the text-extraction check.
6. README with setup, env vars, deployment and backup (the SQLite file is the whole state).

## Places (home / office / vendors)
- `known_sites.kind` is client | home | office | vendor; `default_status` is Draft or Personal.
  Stops detected on a home/office/vendor place are pre-filled with its name and (for Home) pre-marked
  Personal, but work can still be done there: the user flips the stop to Confirmed. Clients are never
  auto-filled. Places are added from a stop, from the phone's last position, or by address search.
- A stop's field is labelled "Work performed" everywhere user-facing (UI, PDF, JSON export key
  `work_performed`); the database column stays `notes`.

## Parts and truck inventory (added after v1 scope was written)
- Per-stop "Parts used": name, part number (sku), quantity, unit cost, billable flag. Each use
  snapshots the catalog values so later catalog edits do not rewrite history.
- `parts` table is both catalog and truck stock (`on_hand`, `reorder_at`, `tracked`). Only tracked
  parts move stock; parts typed ad hoc into a stop are cataloged untracked. Stock may go negative.
  `inventory_log` records every change. Deleting/re-detecting a stop returns its parts to the truck.
- CSV import/export on the /truck page; format documented at the top of `app/parts.py`
  (name required; sku, unit, unit_cost, billable, category, on_hand, reorder_at). The user supplies
  the spreadsheet and formats it to match.
- PDF/JSON export (Milestone 5) must include each confirmed stop's parts (name, sku, qty, unit,
  unit cost, billable) in the stop block and in the "Records to update" table, because the
  HubSpot session needs the list of parts used.

## Added after v1 scope (all built)
- Recorder address/user/device/login are Settings-page values (env vars are only defaults); "Find on
  Recorder" lists users/devices via `/api/0/list` and `/api/0/last`.
- Daily SQLite snapshots in `/data/backups` (`BACKUP_KEEP`), Back up now + download in Settings.
- Live status banner (`/api/live`): last position, place name or OSM address, time at the spot.
- Day start/end marks (`day_marks`): optional; shown in the report (`day_start`, `day_end`, schema stays "1"),
  and they override the work-hours window for no-data alerts. They also decide the commute: the day's first drive out
  of a Personal stop (home) counts if Start day was tapped before it ended, and the last drive into a Personal stop
  counts if End day was tapped before it ended (`service.drive_counts`); mid-day Personal stops stay excluded.
- Undo (`undo_log`, last 10 per day) for delete, merge, split, re-detect and Confirm all; one-tap confirm for
  Draft stops that sit on a saved place; "usual parts" per business.
- Alerts (Pushover, generic text only): no data, Recorder down, end-of-day Draft review, low truck stock.
- Debug data download (`/api/debug/<date>.json`): raw points + stops + edits, for reviewing a day offline.

## Working agreements
- Ask the user before choosing Google Places over OSM, and before anything that costs money.
- Show each milestone working before starting the next.
- Keep dependencies few and pinned; write a short README as you go.
- Use sample data until the live Recorder is confirmed reachable; never hardcode
  credentials or the user's addresses in the repo.
