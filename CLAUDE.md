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
  and they override the work-hours window for no-data alerts. They also decide the commute (`service.drive_counts`):
  the day's first drive out of a Personal stop (home) counts if Start day was set before it ended (not if set on arrival);
  the drive INTO the stop where the day ended counts when that stop is Personal (home, or an errand on the way home), so
  work time ends on arriving there; drives after it never count. Ending at a customer or vendor stops the clock when you
  leave it: the drive home does not count as work time. Mid-day Personal stops stay excluded.
- **Mileage is always Home -> work stops in order -> Home** (`service.mileage`, report `mileage` + `summary.miles_approx`),
  independent of the time rules and of Start/End taps, because it is what a customer may be billed. Personal and Lunch stops
  are left out: a detour through one is replaced by the direct road distance; Traffic stops are summed from the real drives;
  legs next to a detected Home stop use the recorded drive. Needs a Home place; until then the old per-drive miles are used.
  `end_day` (time, kind home|personal|job, miles_to_home = last work stop to Home, method, counted = drive home counted as time).
- Miles to Home come from `app/routing.py` (public OSRM by default, `ROUTING_URL`; blank = straight-line x1.25 estimate).
  Coordinates are rounded to ~100 m before leaving the server and routes are cached (`route_cache`).
- Stop statuses are Draft, Confirmed, Personal, Lunch, Traffic. Lunch: left out of on-site time, the drives to and
  from it do not count, and the report lists it by time only ("Lunch"). Traffic: a slow-down detected as a stop; not on-site
  time, its minutes are added to drive time (`traffic_minutes` in the report summary). The Export panel blocks PDF/JSON
  while any stop is still Draft (an "Export anyway" tickbox overrides). Start day within 2 minutes of arriving
  (`START_SLACK_S`) means you started on arrival, so that commute is not counted.
- Sync new data, Confirm all known and Undo also live on the Export & tools page ("Day actions"). The day toolbar has only
  Start/End day, End at home, + Add stop and Day times.
- A stop card has the status buttons and Save / Confirm / More (Split, Start day here, End day here, Merge, Delete) at the top.
  "Start day here" marks every earlier stop Personal and "End day here" every later stop Personal (undoable).
- Once End day is set, Sync / re-detect add no stops that begin after it, and + Add stop refuses them (clear End in Day times to allow).
- Export, Re-detect and Debug data live on the **Export & tools** page (`/tools?date=`, linked from the header on every page), not on the day page. Tapping End day with no Drafts left jumps there.
- Any stop can be the start or end of the day ("Start day here" = arrival; "End day here" = departure, or arrival for a
  Personal stop). **End at home** ends the day at the saved Home place: it uses a detected Personal Home stop if the day
  ends there, otherwise adds a one-minute manual Personal Home stop (distance to it is estimated straight-line x1.25).
- Places are saved automatically: Confirming learns the business; a named Personal stop is saved as a place (home if the
  name says so). There is no "Save as place" button on the day page (the Places page still manages them).
- The day toolbar sits at the top of the day page in a 3-column grid.
- PDF layout (single day): one top section (date, timezone, generated, schema, totals, work day, End Day), a Mileage table
  (label/value rows, no notes, total last), stop blocks whose "Drive from previous stop" is the mileage leg ending at that
  stop (`drive_from_previous_stop.from`, minutes only when the drive counted as work time), Records to update, and a
  **Day recap (timesheet)**: In / Out / Activity / Hours rows (on-site visits, counted drives, traffic, unpaid lunch in
  brackets), then On site, Drive, Total paid hours, unpaid lunch and the Home-to-Home mileage (JSON `timesheet`). Personal stops
  are never listed in the PDF (the JSON `excluded` list still has them). The document is titled "JobHub Daily Summary"
  (files `jobhub-daily-summary-<date>`). The report's final counted drive is the drive INTO the End Day stop, even when
  later stops follow it.
- Lunch is PAID to the tech but not billable to the customer. Two cases. (1) A visit, a Lunch (or Personal) stop, then the
  same site again (`service.same_site`: same saved place, or same name within 300 m) is ONE stop in the report: first
  arrival, last departure, work notes/parts combined, `lunch_non_billable` / `personal_breaks_unpaid` [{from,to,minutes}] and
  Time on site net of the gap; the trip adds no drive time or miles. (2) Lunch between two different work stops is part of
  the trip: the drives before and after count, the stop after lunch shows them added together as one drive, and the miles
  are the direct leg. Lunch next to home is not listed or paid.
- A Personal stop between two work stops is a clock-out: the whole gap (leave one job, arrive at the next) is an unpaid
  "Clocked out (personal break)" row; mileage skips it (direct leg). Home before and after the day is not a break.
- Summary additive fields: `lunch_minutes`, `break_minutes_unpaid`, `paid_minutes` (on-site + lunch + drive). Top PDF
  section has no timezone / generated-at / schema / drive-after-last-stop rows (the timezone is in the notes, the schema
  version in the footer). Order: top, stops, Records to update, Mileage, Day recap (times in "3 h 48 min" form).
- The stop block's "Drive from previous stop" names the KIND of place the drive started from (Home, Client, Office, Vendor;
  JSON `from_type`), not its name; the JSON still carries the name in `from`. In the Day recap the lunch rows and total just say "Lunch"
  (the top section keeps "Lunch (paid, not billed)").
- Parts CSV import has a "Check file" dry run (`/api/parts/import?dry=1`, runs on an in-memory copy of the database).
- **Notes & parts sheet** (day page): each Draft/Confirmed stop card has a "Notes & parts" button that slides a sheet up. Work
  performed, Category, Job/ticket/project and Visit result autosave ~2 s after typing stops (and on leaving a field); a status line
  reads Unsaved changes / Saving… / Saved <time>. **Done** saves and closes. Parts, expenses and follow-ups save as they are added.
  Cards show "Needs notes / category" on Confirmed stops that lack them. The card's own Save no longer carries notes.
- Stop job fields (`stops.work_category`, `ticket`, `visit_result`; `stops.category` is the OSM place type, do not reuse):
  `service.WORK_CATEGORIES` (the SOP's seven; value = billing: hourly | project | non_billable), `VISIT_RESULTS`
  (complete | return | waiting). Confirming a stop with no category copies the one last used for the same business
  (`last_category`; the sheet also offers it as a button). Follow-ups are a `follow_ups` table (stop_id, text, owner, due),
  included in Undo snapshots, moved on merge, deleted with the stop; the sheet adds them with owner "Me".
- Expenses are parts with `kind='expense'` (parking, tolls, purchases): never tracked, no stock, not billable unless ticked; each use
  takes its own amount (`unit_cost`) and two uses stay two lines. CSV `type` accepts expense. Report: `expenses` per stop, an Expenses
  table in the stop block, `summary.expenses_total`; they never appear in `parts_used`.
- Report additions (schema stays "1"): per stop `category`, `billing`, `job_reference`, `visit_result`, `follow_ups`, `expenses`;
  `summary.hours_by_category` (on-site minutes only) and `expenses_total`; top-level `follow_ups`; records_to_update has category and
  job_reference. PDF: "Hours by category" under the top section, Category / Job / Visit result rows, a Follow-ups section after
  Records to update. `/api/export/check` returns `warnings` (no category, empty work performed, follow-up without owner or due date);
  the Export page lists them but never blocks. Times under an hour print as "50 min"; the recap stays on one page.
- Undo (`undo_log`, last 10 per day) for delete, merge, split, re-detect and Confirm all; one-tap confirm for
  Draft stops that sit on a saved place; "usual parts" per business.
- Alerts (Pushover, generic text only): no data, Recorder down, end-of-day Draft review, low truck stock.
- Debug data download (`/api/debug/<date>.json`): raw points + stops + edits, for reviewing a day offline.

## Consumables, truck count and shopping list (added later)
- `parts.kind` is `part` or `consumable` (tape, solvent, gloves: used up, not billed). Consumables default to
  not billable; the CSV has `type` and `vendor` columns. Use is optional per stop; a weekly **Count the truck**
  (`/api/parts/count`) sets real quantities and shows "used since last count". The **Shopping list** groups
  tracked items at or below reorder level by vendor (suggests restocking to 2x the reorder level).
- Report: each stop's `parts_used` holds parts only; consumables are in `consumables_used` (JSON) and a separate
  "Non-billable materials" table in the PDF; "Records to update" lists parts only. Schema stays "1" (additive).
- Planned, not built: purchase/restock log with cost and a consumables cost summary (phase 3); install kits (phase 4).

## Parts: units, packs, dropdowns (built) and the action plan (not built)
- `parts.unit` is one of `parts.UNITS` (ea, pair, pack, box, bag, case, roll, ft, gal, lb); the Truck page uses a dropdown, the API and
  CSV accept common spellings (each, feet, bx...) and REJECT anything else (an older stored unit still saves while unchanged).
  Stock is always counted in `unit`. `pack_size` + `pack_unit` describe how it is bought ("bag" of 25): the stock row has
  "+ Add bag (25)" (`/adjust {packs}`), and the shopping list rounds up to whole packs ("1 bag of 25 (25 ea)").
- Vendor and Category are dropdowns with "+ New…" (`/api/parts/choices`: vendors = saved vendor Places plus vendors on parts;
  categories = those already used). Stock buttons read "+ Add stock", "− Remove stock", "Set stock count to". The Truck page has a
  Low stock tab and "Show removed items" (with Restore).
- Action plan for parts, in the order to build (1 and 2 built, 3-7 not yet):
  1. (built) New-part prompt on the job sheet: typing a part that is not in the catalog asks for unit and cost, so the PDF is never blank there.
  2. (built) Export warning for billable parts with no unit cost (same list as the other export warnings).
  3. Bin / location field ("Shelf 2"); the truck count sheet sorts by it.
  4. Shopping list: "Mark bought" (adds the stock, in packs) and a share/copy text that pastes cleanly into Notes.
  5. Purchase log (phase 3): "+ Add stock" also records what was paid and from whom; last cost updates the part; consumables cost summary.
  6. Possible-duplicate check when adding (similar name or part #).
  7. Install kits (phase 4): one tap adds a bundle of parts to a stop.
  8. (built: Google Vision) Read receipts: suggest fuel total/gallons and store totals; later an AI reader as option 3.

## Notes: spell and grammar check (built)
- The notes box sets spellcheck / autocorrect / autocapitalize explicitly (the iPhone keyboard does the spelling).
- Grammar: **LanguageTool** (open source), self-hosted as its own container so notes stay on the network. `app/grammar.py`;
  `LANGUAGETOOL_URL` env or Settings → Notes (blank = off, and the button is hidden). `POST /api/grammar {text}` forwards ONLY the
  text to `{url}/v2/check` (language en-US, 5000 chars, 30 issues) and returns offset/length/message/replacements;
  `GET /api/grammar/status` says whether it is on. The sheet's "Check grammar" button lists issues with one-tap fixes.
- Community image `erikvl87/languagetool` (port 8010, about 1 GB RAM) is a commented example in docker-compose.yml; confirm it
  before relying on it. Not planned unless asked: an AI "polish my notes" button (sends customer notes outside, costs per use).

## Data gaps, open stops, vendor marking, auto-sync (built; from the 2026-11-20 day)
Found on 2026-11-20: the phone sent nothing 08:54:55 to 09:55:12; the app drew a 60-minute counted "drive" for 2.7 km, and the tech
then hand-edited times (which froze the stop). The export also said "from Client" for a Home Depot stop, End Day 12:00 while the last
stop departed 12:21, and listed billable parts with no cost.
- **Data gaps** (`service.data_gap`, `_gap_estimate`): a drive whose points have a hole longer than STOP_MAX_GAP_MIN counts as
  distance / ~25 mph (min 5 min) ending when the next point arrived, not the whole silence. Not applied when you typed the previous
  stop's departure or the next stop's arrival, or edited the drive (`_times_typed`). Drive payload `gap {from,to,minutes}` and
  `estimated`; the day view shows "No location data 8:54 – 9:55" under the drive; report drive has `estimated` and
  `no_location_data`; PDF says "(estimated: no location data)"; the recap row is labelled too.
- **Still on site**: `stops.arrive_set` / `depart_set` pin only the side you edited (`times_edited` still pins both, for split/merge/
  manual). Sync keeps growing an unpinned side. Payload `here_now` (last stop, newest point under 15 min old and inside it) shows "Here now"
  and a **Leaving now** button (`POST /api/stops/<id>/leave`, today only). Changing the departure of the stop that ended the day moves the
  End Day mark with it (a Personal/off-clock stop does not).
- **Save as place**: the stop card's More menu has Save as Vendor / Office / Home (`POST /api/stops/<id>/save-place`). `vendor_hint`
  offers a one-tap "Save as Vendor" when the name matches a vendor used on parts or the place type is a supply shop. Confirming a stop on
  a Vendor place with no category sets Internal operations. A named Personal stop is no longer silently saved as a vendor (only a name
  containing "home" is saved, as Home).
- **Auto-sync**: Settings, OwnTracks tab, "Auto-sync every (min)" (`AUTO_SYNC_MIN`, default 2, 0 = off). While TODAY's page is open and
  visible the page POSTs `/api/day/<date>/auto-sync`; the server checks `last_tst` and fetches only points after the newest stored one
  (`points.load_day(..., incremental=True)`), then runs `sync_new`. It does nothing while the notes sheet, a form or a typed field is
  open. No server-side background sync for stops (the 60 s alert thread only watches `last_tst`).
- **Export warnings added**: estimated drive, billable part with no unit cost, length-type part counted in "ea" (cable, wire, pipe...),
  End Day earlier than the last departure.
- Phone-side (not code): OwnTracks Move mode, Location Always, Background App Refresh on, Low Power Mode off (the gap looked like iOS
  freezing the app).

## Stop types, fuel, photos, export filters (built 2026-11-22)
- **Stop type first** (`stops.stop_type`: job | vendor | office | gas | lunch | personal | traffic; `service.STOP_TYPES`). The
  time rules still run on `status`: gas/lunch/personal/traffic set it (`TYPE_STATUS`; **Gas = Traffic**: pump time is drive
  time, mileage follows the real drive), job/vendor/office stay Draft until confirmed. `service.stop_type()` derives a type for
  rows without one (status, then saved-place kind; Confirmed = job; '' = not chosen). Setting `status` the old way keeps
  `stop_type` in step. Confirming a vendor-type stop with no category sets Internal operations.
- **Stop sheet** (day.js `openSheet`) is the one place to edit a Job/Vendor/Office/Gas stop, in three sections: 1 What and
  where (type buttons, business/station + Find, address, Arrived/Left, Here now + Leaving now), 2 Work (Work performed,
  grammar, category with "last time here", parts, vendor purchases, photos, details: ticket/expenses/follow-ups; Gas: fuel +
  receipts), 3 Finish (visit result for jobs, due date for a return/waiting follow-up, "Still needed" links that jump to the
  field, **Finish stop**, "Save & close (finish later)"). Stop tools (split, merge, delete, start/end day here, save as a place) open from a
  **Tools** button in the last slot of the type grid (under Gas), on the sheet and on the card. All typed fields autosave (FIELDS include business_name,
  address, category, known_site_id, arrive, depart). Closing always returns to the list on that stop.
- **Saved places by type**: the sheet has a "Saved clients / vendors / offices (n)…" dropdown (Job = client places, Vendor = vendor,
  Office = office; nearest first with distance) that fills name, address and `known_site_id`. Picking the Office type fills the
  nearest saved Office, and Personal fills Home when the stop is within 300 m of it (`service._place_for_type`), only when the stop has
  no name yet or its name came from a saved place of another kind; a typed name is never replaced.
- **Finish** `POST /api/stops/<id>/finish {visit_result, due}` (`service.finish_stop`): checks `service.stop_needs` (job:
  business name, category unless last time's can be copied, work performed, visit result; vendor/office: name; gas: total
  paid), adds one follow-up "Return visit" / "Waiting on parts or customer" (owner Me, due) when that is the result, confirms.
  It never sets the departure: a confirmed stop keeps growing on sync until you type the departure or tap Leaving now, so you
  can finish before driving away (tested). Payload `needs` drives the card and the sheet. Export warns on a billable job with
  no visit result.
- **Card** is a summary: time, name, "Needs …", type chip and one button: **Start job** + Other type… (no type yet),
  Continue / Fill-up / Review (opens the sheet), plus Confirm as <saved place> and Save as Vendor. Lunch/Personal/Traffic and
  untyped stops open a small area on the card (type buttons, times, Personal name, Stop tools); picking a sheet type opens the
  sheet. "Finished" = confirmed (gas: has a total) with nothing in `needs`.
- **Fuel** (`fuel` table, one row per Gas stop: gallons, total, odometer; `PUT /api/stops/<id>/fuel`). `GET /api/fuel?from&to`:
  fills newest first with MPG from the odometer change since the previous fill-up (full-tank method, gaps > 1500 mi ignored) and a
  summary (spend, gallons, avg $/gal, MPG, $/mile). Truck page → Fuel. Report: day `fuel` list, `summary.fuel_total`, a PDF
  Fuel table; gas stops are excluded as "Fuel stop (counted as drive time)" and the recap row says "Fuel stop (name)". Export
  warns on a fill-up with no total. Manual stops (+ Add stop) can be given a type.
- **Photos/receipts** (`app/photos.py`): files in `<data>/photos/YYYY/MM/DD/<id>-<kind>.jpg` by the stop's date; the `photos`
  table keeps `path` (`data` is empty; the first version's BLOBs move to files on connect, `move_to_files`). The SQLite
  snapshots do not include them: back up `/data/photos/` too. Kept when a stop is deleted (user wants them kept). The phone shrinks to 1600 px,
  the server re-encodes with Pillow (upright, 1600 px, JPEG, no EXIF/GPS), 12 per stop. `POST /api/stops/<id>/photos`
  (multipart file, kind receipt|photo), `GET /api/photos/<id>.jpg`, `DELETE /api/photos/<id>`. Merge moves them, re-detect
  re-attaches them to the new stop at the same time, delete leaves them so Undo restores them (orphans stay in the DB). Report:
  per stop `photos` (ids), day `photos` [{id, kind, caption, label}] for confirmed stops and fill-ups only (never Personal,
  Lunch or unconfirmed), PDF "Receipts and photos" section before the reading notes. The card's viewer is an in-page overlay.
  Each report photo has `file` = its name in the zip (`photos/YYYY/MM/DD/stop-<n>-<business-slug>-<kind>-<k>.jpg`, fuel stops
  `fuel-<hhmm>-<slug>-...`); the PDF prints it under the label.
- **Zip export** `GET /api/export.zip?from&to&omit`: `<name>.pdf`, `<name>.json` and the report's pictures at their `file`
  paths (JPEGs stored uncompressed). Export page button "Zip: PDF + photos" (share sheet in Home Screen mode).
- **Export filters**: `omit=` (comma list of `report.OMIT_SECTIONS`: costs, expenses, fuel, photos, materials, categories,
  follow_ups, mileage, recap) strips them from PDF and JSON; `omitted` [{key,label}] and a reading-note line say what was left
  out on purpose, and notes for absent sections are dropped. `/api/export/check` ignores omit. Export page "Include" ticks
  are remembered per phone.
- **Home Screen mode**: links with `data-file` (exports, debug, parts CSV, backups) go through `static/share.js`: a tap opens a
  panel with **Open in Safari** (the URL carries `ft=`, a signed download token from `GET /api/file-token`, 10 min,
  `FILE_TOKEN_S`, accepted by the login gate only on GET of `FILE_PATHS` download routes, because Safari has no login cookie;
  PDFs open with `?inline=1`) and **Share / Save to Files** (the file is fetched while the panel is open; iOS needs a fresh tap).
- Not built: OCR of receipts; auto-detecting gas stations for stops that have no place category yet (only `gas_hint` from a
  picked fuel place); cleanup of orphaned photos.

## Alerts, find, parts, backups (built 2026-11-22, later)
- **Unfinished stop alert** (`ALERT_LEFT_STOP` / Settings → Alerts, default on): during the work day (work hours or after Start
  day, until End day) the alert thread runs `service.catch_up` (the same incremental sync as the page, behind a lock) every
  AUTO_SYNC_MIN minutes (5 if that is 0), then `service.left_unfinished`: a work stop (job/vendor/office/gas or untyped, not
  Personal/Lunch/Traffic) with newer points 5+ min after its departure, left in the last 4 h, arrived after Start day, with
  `needs` or not Confirmed. One Pushover per stop (`left_stop:<id>`, never repeated), generic text (arrival time and what is
  missing, no names or places); the tap opens `/day/<date>?stop=<id>`, which opens that stop's sheet.
- **Find business** searches for what is typed in the name field (else the address) near the stop; empty = nearby. No second
  search box; "Show what is nearby instead" is offered.
- **New-part prompt**: adding a part name that is not in the catalog opens a small form (counted in, unit cost, part/consumable,
  part #); a billable part needs a cost. The part is added to the catalog (untracked) with that unit and cost.
- **Backups**: database snapshots keep 7 by default (`BACKUP_KEEP`). Settings → Backups → **Download everything** streams
  `/settings/backup-full.zip` (a consistent DB copy + `photos/`, built in an unlinked temp file). Photos are not copied anywhere
  automatically; off-site (S3 / Linode Object Storage / Google Drive) is not built (costs money: ask first).

## Places history, menu bar, calendar (built 2026-11-22, later)
- **Menu bar**: one partial `templates/_nav.html` on every page (Day, Export, Places, Truck, Settings); the current page is a
  filled pill (`aria-current`). The day page includes it inside its own header (date bar below). Export keeps `id=toolslink`.
- **Places history**: tapping a place shows its visits first (`GET /api/places/<id>/history`: stops linked to the place, plus
  unlinked same-name stops within 300 m), newest first, totals, 10 then "Show all"; each visit links to `/day/<date>?stop=<id>`.
  "Edit place" is folded below. Gas stations are not saved places: the filter "Gas stations (from fill-ups)" lists them from
  Gas stops grouped by name (`/api/gas-stations`, `/api/gas-stations/history?key=`), with fill-ups, spend and avg $/gal.
- **Calendar** (`app/calendar_feed.py`): Office 365 / Outlook "Publish a calendar" ICS link in Settings → Calendar (secret,
  write-only, up to 1000 chars, webcal:// accepted) and a keyword (default `onsite`; on-site / on site match) searched in
  title, location, notes and categories. All-day and cancelled events skipped; recurring events expanded
  (`recurring-ical-events`); Outlook's Windows time zone blocks work. Feed cached 10 min in memory. Event notes are only
  searched for the keyword and a ticket number, never stored. `day_payload.calendar` lists the day's on-site events with the
  matched stop (most overlap, +/- 30 min, one event per stop, never Personal/Lunch/Traffic) and state done | here | upcoming |
  missed; a matched stop whose name differs gets `calendar` {name, location, ticket}, shown as "📅 Use <name>" on the card and
  "Use calendar" in the sheet (fills name, address, ticket if empty; an untyped stop becomes a Job). Calendar errors only show a
  message (`calendar_error`), never break the day. Settings has "Test calendar". Address format: to be confirmed by the user.
- **Receipt reading** (`app/receipts.py`, Settings → Receipts; `RECEIPT_READER` off | google_vision, `RECEIPT_KEY` blank = the
  Google Places key, `RECEIPT_MONTHLY_CAP` 300, counted in `api_usage` provider "receipts"). On a receipt upload the reader runs once
  and stores `photos.read_json` {ok, total, gallons, price_per_gallon, checks | error, dismissed}; plain photos are never sent.
  Vision: DOCUMENT_TEXT_DETECTION, then `receipts.parse` (TOTAL/SALE line not subtotal/tax/change, GAL, PRICE/GAL, fills in the third
  from the other two, `checks` = gallons x price within 2% of the total). The sheet shows "Receipt says …" with **Use these** (gas: PUT
  fuel) or **Add $x as an expense** (other stops: expense "Store purchase"), Ignore (`/dismiss-read`), Try again (`/read`). Never
  fills anything without a tap. An AI reader (option 3) is a future entry in `receipts.READERS` returning {text, fields}.
- Not built yet: summary home page with time to the next appointment and ETA while driving (needs geocoding the event address;
  free OSRM has no traffic, Google Routes costs money: ask). Google Drive photo backup needs OAuth (an API key cannot write to
  Drive); not built.

## Working agreements
- Ask the user before choosing Google Places over OSM, and before anything that costs money.
- Show each milestone working before starting the next.
- Keep dependencies few and pinned; write a short README as you go.
- Use sample data until the live Recorder is confirmed reachable; never hardcode
  credentials or the user's addresses in the repo.
