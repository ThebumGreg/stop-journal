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
- Calendar invites (user-approved 2026-11-23, Settings → Email, off by default) email today's stops as Outlook invites to the
  user's work address every 5-10 min: type, customer name, address, ticket, times, finished. Never notes, parts or prices.
- Email report (user-approved 2026-11-23) sends the full report (names, addresses, notes, coordinates unless left out)
  to the user's own work address, ONLY when the user taps Email report. Never send it automatically.
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
- Purchase log and install kits are built (see the parts action plan below).

## Parts: units, packs, dropdowns and the action plan (all built)
- `parts.unit` is one of `parts.UNITS` (ea, pair, pack, box, bag, case, roll, ft, gal, lb); the Truck page uses a dropdown, the API and
  CSV accept common spellings (each, feet, bx...) and REJECT anything else (an older stored unit still saves while unchanged).
  Stock is always counted in `unit`. `pack_size` + `pack_unit` describe how it is bought ("bag" of 25): the stock row has
  "+ Add bag (25)" (`/adjust {packs}`), and the shopping list rounds up to whole packs ("1 bag of 25 (25 ea)").
- Vendor and Category are dropdowns with "+ New…" (`/api/parts/choices`: vendors = saved vendor Places plus vendors on parts;
  categories = those already used). Stock buttons read "+ Add stock", "− Remove stock", "Set stock count to". The Truck page has a
  Low stock tab and "Show removed items" (with Restore).
- Action plan for parts, in the order built (all built):
  1. (built) New-part prompt on the job sheet: typing a part that is not in the catalog asks for unit and cost, so the PDF is never blank there.
  2. (built) Export warning for billable parts with no unit cost (same list as the other export warnings).
  3. (built) Location is two fields: `parts.shelf` ("Shelf 2", "Door"; a datalist of shelves already used) and `parts.bin` ("12").
     CSV columns `shelf`, `bin` (aliases location/rack → shelf, drawer → bin). Count the truck sorts shelf, then bin (natural
     order, blanks last), one section per shelf with shelf buttons on top (typed counts are kept across shelves). The first
     single field was split once on connect (`parts.split_shelf_bin`, marker `_split_shelf_bin`: "Bin…"/numbers stay bins,
     anything else becomes the shelf).
  4. (built) Shopping list: per line an editable amount (packs for pack items) and ✓, "✓ All bought" per vendor
     (`POST /api/parts/bought {vendor, items:[{id, packs|qty}]}`, one transaction, logged as reason "bought" with "Bought at
     <vendor>"), an Undo that removes it again, and **Share list** (iOS share sheet, else copy): a ☐ checklist by vendor.
  5. (built) Purchase log (`purchases` table: part, ts, vendor, qty in units, total, note). Recorded by shopping-list Bought (optional
     "$ paid" per line) and by + Add stock / + Add pack with "Paid" + "Bought from" (`adjust(..., paid, vendor)` logs reason
     "bought"). The part's `last_cost` = total / qty; its `unit_cost` (the price in reports) is NOT changed; the part page shows
     "Last paid $x · you charge $y (+z%)" (red if below cost). `GET /api/purchases?from&to&vendor&part`: list, total, by_vendor,
     by_kind (part / consumable), unpriced count. Truck → Stock → Purchases (ranges, tiles, by vendor, list); vendor pages show
     their purchases and total; vendor rename updates purchases.
  6. (built) Possible-duplicate check (`parts.similar`, `GET /api/parts/similar?name&sku&exclude`: same part # ignoring case/spaces/
     dashes, or names that are nearly the same, same words in any order, or contained): the new-part sheet warns with links, and
     the stop sheet's new-part prompt offers "Use <existing part>".
  7. (built) Install kits (`app/kits.py`; tables `kits` (name unique, notes) and `kit_items` (part, qty)). Truck → Kits (#kits,
     #kit-<id>, #kit-new): list with the parts total at unit costs and "short" when the truck has less than one kit needs; edit
     name, notes and lines (part by name or #, qty). Expenses cannot be in a kit. `POST /api/stops/<id>/kits/<kit> {times}` adds
     each active line through `parts.add_to_stop` (catalog price, stock moves, an existing line gets the quantity added). The
     stop sheet's Parts block has "Add a kit…" when kits exist.
  8. (built: Google Vision) Read receipts: suggest fuel total/gallons and store totals; later an AI reader as option 3.

## Notes: spell and grammar check (built)
- The notes box sets spellcheck / autocorrect / autocapitalize explicitly (the iPhone keyboard does the spelling).
- Grammar: **LanguageTool** (open source), self-hosted as its own container so notes stay on the network. `app/grammar.py`;
  `LANGUAGETOOL_URL` env or Settings → Notes (blank = off, and the button is hidden). `POST /api/grammar {text}` forwards ONLY the
  text to `{url}/v2/check` (language en-US, 5000 chars, 30 issues) and returns offset/length/message/replacements;
  `GET /api/grammar/status` says whether it is on. The sheet's "Check grammar" button lists issues with one-tap fixes.
- Community image `erikvl87/languagetool` (port 8010, about 1 GB RAM) is a commented example in docker-compose.yml; confirm it
  before relying on it. The AI "polish my notes" button is built (see AI notes).

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
- **Stop type first** (`stops.stop_type`: job | vendor | office | gas | break | lunch | personal | traffic; `service.STOP_TYPES`).
  **Break** (Short break: restroom, coffee) runs on the Traffic rules like Gas: paid, counted as drive time, not on-site time;
  the report says "Short break (paid, counted as drive time)". Lunch stays paid but not billed (see Lunch rules). The
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
  panel with **View here** (PDFs: an in-page viewer with Share and Close showing `/api/export.view`: every PDF page
  rendered to a PNG by `app/pdfview.py` with `pypdfium2`, stacked in one HTML page, max 60 pages; iOS draws only page 1 of a PDF
  in a Home Screen app), **Share / Save to Files** (only on https: iOS has no Web Share on plain http; the file is fetched while
  the panel is open) and **Copy link for Safari** (the URL with `ft=`, the 10-minute download token, to paste into Safari). There is
  no Open in Safari: window.open stays inside the app (manifest scope `/`) and iOS rejects `x-safari-https://` there.
  Plan: serve JobHub over https (Tailscale serve or a reverse proxy) so Share works in the app.
- **Gas station detection** (`service.check_fuel_stops`, `stops.fuel_check` NULL/0/1): on day load and when auto-sync adds data,
  up to 4 short (≤ 30 min), untyped, unnamed Draft stops not on a saved place get one OSM lookup each (60 m; `amenity=fuel` wins
  over a shop tag in `osm._category`). A hit sets `gas_hint`; the card asks "⛽ At a gas station. What was it?" with Fill-up
  (Gas), ☕ Short break and Job here. Never sets the type itself; a failed lookup is retried later. Skipped
  when tests pass a client without a provider.
- Not built: cleanup of orphaned photos (the user keeps them for now).

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
- Calendar notes: labelled lines `Ticket:`, `Contact:` (or Phone), `Work:` (or Scope), `Category:` (or Billing; must match one of
  WORK_CATEGORIES, any case) are read (`calendar_feed.labelled`, first of each, 200 chars); the notes are never stored whole. A
  matched stop always carries `calendar` {name, location, ticket, contact, work, category, start, end, offer}; the sheet shows an
  Appointment box (planned work, contact with 📞 Call), and "Use calendar" (offer = names differ) also fills an empty category.
  Recommended event format: title "Onsite – <customer>", Location = full street address, those labelled lines at the top of notes.
- **Receipt reading** (`app/receipts.py`, Settings → Receipts; `RECEIPT_READER` off | google_vision, `RECEIPT_KEY` blank = the
  Google Places key, `RECEIPT_MONTHLY_CAP` 300, counted in `api_usage` provider "receipts"). On a receipt upload the reader runs once
  and stores `photos.read_json` {ok, total, gallons, price_per_gallon, checks | error, dismissed}; plain photos are never sent.
  Vision: DOCUMENT_TEXT_DETECTION, then `receipts.parse` (TOTAL/SALE line not subtotal/tax/change, GAL, PRICE/GAL, fills in the third
  from the other two, `checks` = gallons x price within 2% of the total). The sheet shows "Receipt says …" with **Use these** (gas: PUT
  fuel) or **Add $x as an expense** (other stops: expense "Store purchase"), Ignore (`/dismiss-read`), Try again (`/read`). Never
  fills anything without a tap. The AI reader (`claude`) is built: see AI notes.
- **Home page** (`/`, `app/summary.py`, `GET /api/summary`; the day view moved to `/day` and `/day/<date>`; menu: Home, Day,
  Export, Places, Truck, Settings; the JobHub brand hides under 520 px). Cards: Now (the stop you are at with its needs, else
  driving / place from the live status), Next appointment (first upcoming on-site calendar event, "in 45 min"; drive time from
  the phone's position: a saved place with the event's name or address first, else OSM search near you; `routing.drive_eta`
  (OSRM duration, rounded coords, cached 10 min in `route_cache.seconds`, else straight line x1.25 at 30 mph); driving = "Arriving
  9:48 · x min early/late", parked = "Leave by" (5 min slack); within 150 m = "You are there"; only for events in the next 4 h;
  Apple Maps directions link), Today (stops, finished, on site, driving, miles, unfinished stops linking to their sheet, the
  day's appointments). The API strips coordinates from the live block. Refreshes every minute while visible. No traffic
  (Google Routes would cost money: ask). Google Drive photo backup needs OAuth (an API key cannot write to
  Drive); not built.

## Stop sheet polish (2026-11-23)
- Section 1 folds once the stop has a name: one line "Type · name · address · 9:12–now" with ✎ Edit (Done); "Here now" and the
  Appointment box stay outside the fold; Still needed links unfold it. Under the times: "Left follows the GPS" or "Left 3:45 was set
  by you" + **Follow GPS again** (`POST /api/stops/<id>/follow-gps`, `service.follow_gps`: detected stops without times_edited;
  clears depart_set and sets the departure to the detected visit that overlaps most; End Day follows like a typed departure).
  Payload `depart_pinned`, `can_follow_gps`.
- Part rows: name with the line total ("$17.50 ($8.75 each)"), − qty + (whole steps; typing any amount still works; − stops at 1,
  ✕ removes), a labelled "Bill" switch.
- With AI notes on, Check grammar (LanguageTool) is hidden: one writing helper.
- Under Work performed: quick-phrase chips (Settings → Notes → Quick phrases, `NOTE_PHRASES` with | separators, kind "lines",
  20 max; `GET /api/note-phrases`) that append to the notes, and on an empty note "↺ Start from last visit (date)" (payload
  `last_notes` from the last Confirmed stop with notes at the same saved place, else same name).
- After Finish the toolbar message offers "Next: Stop 4 needs notes ›" (the next stop with needs, later ones first), opening it.
  The Grammar settings tab is now "Notes" (Quick phrases + LanguageTool).

## Tools and equipment (2026-11-23)
- Purpose: document purchases of owned tools (about 100); no loans, no calibration, not linked to stops. `app/equipment.py`;
  tables `tools` (name, make, model, serial, category, purchased, price, vendor, warranty_until, location truck|shop|repair|lost|
  retired, notes) and `tool_files` (receipt|photo, `photos/tools/<tool>-<file>-<kind>.jpg`, re-encoded without metadata, max 8;
  in the full backup zip). Truck → Tools (#tools, #tool-<id>, #tool-new): tiles (tools in use, their value, bought this year),
  search, where chips, 🧾 when a receipt is attached, "n tools have no receipt"; tool page with purchase details, warranty,
  receipt/photo upload (shrunk on the phone) and an in-page viewer. Import / export tab: tools CSV import (Check file), export,
  template (columns in `equipment.COLUMNS`, aliases like Brand, Serial #, Cost, Date, Where; matched by serial, else name+model).
  Tool vendors appear in vendor lists; vendor pages list "Tools bought here"; vendor rename updates tools.

## AI notes (2026-11-23, the user approved the cost)
- `app/ai.py`, Settings → AI: `ANTHROPIC_API_KEY` (secret, write-only), `AI_NOTES` on/off, `AI_MONTHLY_CAP` 300 (counted in
  `api_usage` provider "ai"). Official SDK `anthropic==2.12.0`, imported lazily. Model `claude-opus-5-5`, effort low, server-side
  fallback on (`betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`); refusal / cut-off / API errors become plain
  messages. ONLY the Work performed text is sent (wrapped in <notes>), with a system prompt that fixes language and never changes
  facts, numbers, part or model names. `POST /api/notes/polish {text}` → {text}; `GET /api/ai/status`. The sheet's
  "✨ Polish notes" (hidden without a key) shows a Suggested box with Use it / Keep mine; nothing is replaced without a tap.
- **AI receipt reader** (Settings → Receipts → `claude`, uses the Settings → AI key; counted against RECEIPT_MONTHLY_CAP):
  `ai.read_receipt` sends the photo with a JSON schema (`output_config.format`, `RECEIPT_SCHEMA`: store, date, total, tax,
  gallons, price_per_gallon, is_fuel, items[{description, sku, qty, unit_price, amount}] ≤ 30), effort low, server-side fallback
  on. `receipts.claude` cleans it (fuel numbers only when is_fuel and in range, checks, date must be YYYY-MM-DD) and returns
  `fields`; `receipts.extract` is shared by stop receipts (`photos.read_json`) and tool receipts (`tool_files.read_json`,
  `/api/tool-files/<id>/read`, `/dismiss-read`). Stop sheet: store/date/tax shown; gas Use these also names the station;
  "Items on the receipt" with + Add per line (an existing part keeps its catalog price; a new part starts at the receipt
  price). Tool page: "Receipt says: $179 · 2026-03-14 · Home Depot" → Use these (price, bought on, vendor) / Ignore.

## Poor signal (2026-11-23)
- The stop sheet writes every change of its typed fields (FIELDS) to localStorage `jobhub-draft-<stop id>` at once (`keepDraft`),
  and clears it when the server has everything. A save that fails for lack of network (`fetch` TypeError or `navigator.onLine`
  false) shows "No signal: kept on this phone…", and closing the sheet then needs no confirmation. `flushDrafts` PATCHes kept
  drafts (not the open sheet's) on `online`, every 30 s, when the page comes back on screen and after a day loads; a draft the
  server refuses (not a network error) is dropped with a message. Opening a stop with a kept draft restores it into the sheet
  (`restoreDraft`) and saves it. Only the PATCH of typed fields is queued (safe to send twice); parts, photos, Finish and other
  actions still need signal and say so.

## Live part search (2026-11-23)
- The stop sheet's Add part / Add expense boxes and the kit editor no longer use `<datalist>` (unreliable on iPhone). `liveSearch`
  (day.js, truck.js) lists up to 8 matches under the box as you type: every word must appear in name, part #, shelf or bin; each
  shows part #, shelf/bin and stock. Tapping a part adds it at once with the quantity in the box; an expense only fills the name
  (it needs an amount). Typing a new name and tapping Add still opens the new-part prompt.

## Branches and relocations (2026-11-23)
- A place is one spot. Several places can share a name: a vendor's branches, or a customer's old and new address (relocation
  jobs). `known.learn` gives a NEW place the stop's type (vendor/office) or the kind of a same-name vendor/office place
  (`branch_kind`); before this, a new branch of a vendor was saved as a client ("from Client" in the PDF). `repair_vendor_branches`
  fixed existing data once on connect (marker setting `_fix_vendor_branches`); customer places are never changed.
- Places page groups same-name places: "Name · n locations" → all visits across them (`GET /api/places/group-history?name=`,
  each visit with its address), then each location (address as the title) with its own history and Edit place.
- **Linked places** (`known_sites.link_id`, `old_address`): Edit place → "Linked to" another place (any name) and "Old address
  (the customer moved from here)". A group is the places sharing link_id (`known.link`; linking two groups merges them,
  unlinking the second-to-last member clears the group; delete unlinks). Places page groups by link first, else by same name;
  the current address comes first and names the group; `group-history?group=<link_id>`. The stop sheet's saved-place list
  labels old addresses.
- Parts keep `vendor` as a name, so a vendor's branches are one vendor for shopping, choices and the coming Vendors tab.

## Truck page layout (redesign, 2026-11-22)
- Menu: Home, Day, **Reports** (was Export; `/tools`), Places, Truck, Settings.
- Truck has tabs in the URL hash: **Parts** (#parts: search, filter chips, Show removed, list; **+ New part** opens a sheet with
  grouped fields), a page per part (#part-<id>: stock with +/−/packs/set, Used on (stops, linked), Edit details, Stock changes from
  `inventory_log`; `GET /api/parts/<id>/history`), **Stock** (#stock: Count the truck, Shopping list), **Vendors**, **Fuel** (#fuel),
  **Kits**, **Tools**, **Import / export** (#data: parts CSV import with Check file, export, template, column list).
- **Vendors tab** (#vendors, #vendor/<name>; `app/vendors.py`): a vendor is a NAME (case-insensitive) joining vendor places (its
  branches) and `parts.vendor`; table `vendors` holds only account, phone, contact, website, notes. List: locations, parts, low
  (low first), last visit. Page: tap-to-call, account/contact/notes, Low at the truck with ✓ Bought, parts (links), locations
  (directions), visits across branches, shopping-list purchases, receipts total (expenses at its stops), Edit. `PUT /api/vendors
  {name, rename, ...}`: rename updates parts and vendor places; renaming onto an existing vendor merges (their details kept,
  gaps filled). Part pages link to their vendor.
- Parts plan: all 7 items and receipt reading are built.

## Working agreements
- Ask the user before choosing Google Places over OSM, and before anything that costs money.
- Show each milestone working before starting the next.
- Keep dependencies few and pinned; write a short README as you go.
- Use sample data until the live Recorder is confirmed reachable; never hardcode
  credentials or the user's addresses in the repo.

## Email report and Go to a place (2026-11-23)
- **Email report** (`app/mailer.py`, Gmail SMTP via smtplib, no new package): Settings → Email (`MAIL_TO`, `SMTP_USER`,
  `SMTP_PASSWORD` secret = Gmail App password, spaces dropped for Gmail; `MAIL_FROM`, `SMTP_HOST` smtp.gmail.com, `SMTP_PORT`
  587 STARTTLS / 465 SSL) with **Send test email** (`/settings/test-email`, uses the form's values). Reports page has an Email
  report box under the downloads: ticks JSON / PDF / Receipts and photos (remembered per phone, `email_opts`), a confirm
  dialog, the last sends for that range. `POST /api/export/email?from&to&omit {pdf,json,photos,anyway}`: same Draft block as
  downloads; subject `JobHub Daily Summary <date>[ to <date>]`, ` (update n)` on later sends (counted from successful sends in
  `email_log`); body `Dates: / Version: / Confirmed stops: / Report schema version: / Attached: / Left out on purpose:` then
  a stop list. Photos are attached until ~17 MB, the rest are counted as skipped. `GET /api/export/emails` → on/ready/to/history.
  The user taps it after checking the day: it is never sent automatically.
- New Include section **GPS coordinates** (`omit=coordinates`): removes latitude/longitude from JSON stops and the
  Latitude/Longitude PDF row (downloads and email).
- **Go to a place** (Home page card): pick a saved place (current addresses only, nearest first from the live position;
  `GET /api/go/places`), then Apple Maps or Google Maps (coordinates if known, else the address). Tapping records a trip
  (`trips` table; `POST /api/go {site_id}`, `DELETE /api/go`; one active, forgotten after 12 h). `service.apply_trip` (run by
  `apply_places` on every detect/sync) fills the first stop that began after the tap (2 min slack) within max(300 m, the place
  radius) of the place: name, address, known_site_id, type from the place kind (client → job, vendor, office, home →
  personal) and its status. Never over a name you typed or an edited/confirmed stop; used once. Home shows "Heading to …" with
  the map buttons and Cancel, then "✓ <place>: stop filled in" linking to the stop. The next appointment card also has both map
  buttons.

## Calendar invites (Phase 1 of the plan below, built 2026-11-23)
- `app/calendar_out.py`: Settings → Email "Put my stops on my work calendar" (`CALENDAR_INVITES` off|on) and "Update every (min)"
  (`CALENDAR_INVITE_MIN`, 5-60, default 10). The background thread (`alerts.run_once` → `calendar_out.tick`) pulls new points
  during the work day and calls `sync(conn, cfg, today)`: one iCalendar REQUEST email per stop (Gmail → MAIL_TO, text/calendar
  alternative + invite.ics), re-sent with SEQUENCE+1 only when the event changes (hash in `cal_sync`), CANCEL when the stop is
  gone. A stop you are at ends at the next quarter hour (about one update per 15 min). Titles `JobHub · <name> (<type>)`, `✓`
  when finished, `· Return visit needed`; Personal / Lunch are bare blocks. UIDs `stop-<id>-<ts>@jobhub`; `calendar_feed`
  skips our events. `cal_sync_log`, max 150 per day. Settings: "Send test calendar invite" (`/settings/test-invite`).

## Action plan (master list, updated 2026-11-23)
The work AI agent is the bridge between HubSpot / QuickBooks and JobHub, by email; no direct connection for now.
QuickBooks makes the estimate (its number); a HubSpot service ticket holds the job and the estimate data. Match on the
HubSpot ticket number (the calendar event's `Ticket:` line, then the stop's ticket); keep the QuickBooks estimate number as info.

A. Status 2026-11-23 evening (user)
  Done: update deployed; the "site down" cause was the router firewall (UniFi IPS, fixed); test calendar invite lands on
  the calendar by itself; Anthropic key loaded; the work agent ran 4 real days.
  Testing 2026-11-24: calendar invites through a work day, the Outlook ICS appointments, the phone app (checklist 7).
  User to do: parts clean-up (checklist 5b), HubSpot company IDs CSV, QuickBooks parts import. Remote access is VPN for
  now; https is undecided (blocks Share in the app and the offline shell).

B. Next to build, in order (user-approved 2026-11-23)
  1. (built) Vendor stops = "Bought here" (see "Bought here" below).
  2. (built) Delete permanently for parts never used on a stop. (Clearing "Our cost" dropped: not needed.)
  3. (built) Late alerts (see "Running late" below).
  Later (user will review): estimates (sample estimate email, IMAP reading, Estimate box + Add quoted parts, quoted vs
  actual in the report); offline app shell after https.

C. Suggested, not chosen yet
  - Weekly / monthly summary; yearly mileage log and year-end accountant package; invoice-ready export.
  - Arrival prompt ("You're at a stop: what is it?"); missing-receipt reminders.
  - Security check before any outside access; code review.

D. Later / parked
  - Read the HubSpot ticket directly (read-only private-app token) instead of estimate emails; Microsoft Graph instead of
    invites (needs IT approval).
  - Cross-check Auris clock-in/out and QuickBooks time entries.
  - Off-site photo backup (costs money: ask); cleanup of photos from deleted stops (kept for now); Google Drive backup (skipped).

## Hand-off review changes (2026-11-23, from the work-agent review of 98b2577)
- **Drive through fuel / break / traffic stops** (report.build_day `chain_minutes`): a work stop's `drive_from_previous_stop.minutes`
  (and the drive into the End Day stop) add every counted drive back through Lunch and Traffic-status stops to the previous
  work stop; time AT those stops stays in `traffic_minutes`. Before, the drive into a fuel stop between jobs was lost from
  drive and paid time. `service.drive_counts.lunch_between_jobs` looks past Traffic stops for the jobs on either side (fuel
  next to lunch made lunch swallow the whole gap). Summary adds `fuel_stop_minutes` and `short_break_minutes` (both included
  in `traffic_minutes`). Invariant test (`tests/test_paid_invariant.py`): paid_minutes = paid timesheet rows; drive_minutes =
  drive rows.
- **Stable stop IDs**: `stops.uid` (UUID4 form), set by the SQLite trigger `stops_uid` on every insert without one (so split's
  new part and every other new stop get one; Undo re-inserts rows with their uid) and backfilled on connect; unique index.
  Merge keeps the earlier stop (its uid); split keeps the uid on the first part; re-detect gives each old uid to the new stop
  covering the middle of the old one (like photos). Report: `stop_uid` in `days[].stops[]`, `records_to_update[]`, top-level
  `follow_ups[]`; `follow_up_id` on every follow-up; day `report_key` = the date. Payload stop `uid`.
- **Technician**: Settings → General → Technician (`TECH_NAME`, `TECH_EMAIL`). Report top-level `technician` {name, email};
  PDF top section "Technician" row when set; email body `Technician:` line after `Dates:`; export warns (never blocks) when either is blank.
- **HubSpot company IDs**: `known_sites.hubspot_company_id` (digits only, `known.hubspot_id`) is edited in Places → Edit place
  (client places) or in bulk: Places → "HubSpot company IDs (CSV)": `GET /api/places/export.csv` (place_id, name, kind, address,
  hubspot_company_id) and `POST /api/places/import[?dry=1]` (matches place_id, else name + address; only that column; blank cell =
  unchanged; never creates places; a bad ID blocks the file). `known.fill_stop_hubspot` copies a place's ID to its CONFIRMED stops
  that have none (run by `service._finish`, place PATCH and import); a stop's ID is never rewritten, except that changing a stop's
  saved place clears it so the new place's ID is copied.
- **Follow-up owners**: Settings → General → Follow-up owners (`FOLLOWUP_OWNERS`, `|`-separated; kind "owners", one `Name = email`
  per line, Me may omit the email and uses the technician email; `settings.parse_owners` / `owner_emails`). Day payload
  `followup_owners` (names); the stop sheet's owner is a dropdown (an older typed owner shows as "(not in the list)"). Report:
  `owner_email` on every follow-up (stop list and top-level); blank when the owner is not in the list, and the export warns.
- **HubSpot ticket # and QuickBooks estimate #** (user decision: two fields). `stops.ticket` is the HubSpot ticket # (sheet label;
  report `hubspot_ticket` = ticket without '#'; `job_reference` stays the raw text); new `stops.qbo_estimate` (sheet "QuickBooks
  estimate #", not on vendor stops; '#' stripped; merge keeps either). Report `hubspot_ticket` and `qbo_estimate` on stops and
  records_to_update; PDF rows "HubSpot ticket #" and "QuickBooks estimate #", Records table column "Ticket / estimate".
  Warnings: Time & Material / Quoted project / White-label migration without a ticket, a ticket that is not digits, and Quoted /
  Migration without an estimate #. Calendar notes: `Estimate:` (or `Quote:`) line fills the estimate like `Ticket:`.
- **Lunch is paid** setting: Settings → General → Pay rules (`LUNCH_PAID` on|off, default on = the rules above). Off: lunch
  timesheet rows are `paid: false`, lunch leaves `paid_minutes`, the PDF says "Lunch (unpaid)" and the recap lists it in brackets.
  `summary.lunch_paid` says which. Mileage is unchanged.
- **QuickBooks parts import**: the parts CSV import recognises a QBO "Products and Services" export ("Product/Service Name"
  header; `parts._qbo_import`): SKU → part number, Sales price / Rate → unit_cost (the price you charge), Purchase cost →
  last_cost (what you pay; the review suggested unit_cost, which would bill at cost), Preferred vendor, Category; "Parent:Child"
  names keep the child; Service / Bundle rows skipped (`services_skipped`); Quantity on hand and Reorder point NOT imported
  (company stock, not the truck); an existing part keeps its kind (QBO has no consumables). Export warns on a billable part
  with no part number.

## Round 2 review (2026-11-23)
- **Parts columns** (A2/A3, user naming): `parts.vendor_item` (supplier part #), `parts.qbo_item` (QuickBooks product/service
  name) and `vendor_unit_cost` = `last_cost` ("Our cost": what you pay per JobHub unit). CSV aliases: `cost`, `our_cost`,
  `purchase_cost`, `vendor_cost`, `dlv_unit_cost`, `last_cost` → vendor_unit_cost (a bare "cost" is NEVER the sell price);
  `dlv_item`, `vendor_sku`… → vendor_item; `qbo_name`… → qbo_item. Import `notes` say how a cost column was read. Export and
  template carry the three columns. Blank cells keep values. `part_dict` adds `margin`, `margin_pct` (of the sell price),
  `below_cost`; Truck list shows margin and a "Below cost" chip; part page "Our cost … · margin/LOSS"; shopping list shows the vendor #.
- **QuickBooks Service items** (A1): `QBO_IMPORT_SERVICE_ITEMS_WITH_SKU` (Settings → General → Parts import, default on):
  Service rows with a SKU import as parts; without a SKU, Bundles and anything named/SKU'd Labor are skipped.
- **Report (B1-B3)**: `summary.billable_on_site_minutes` (on-site minutes of stops whose billing is not non_billable); PDF top
  shows "On-site time" and "Billable on-site time". Each `days[].photos[]` has `attachment` (the exact email attachment name,
  `report.attachment_name`, also used by the mailer). `records_to_update[]` adds billing, visit_result, time_on_site_minutes,
  technician_email, consumables_used and expenses (short strings), photo_attachments. Parts carry `qbo_item` (from the catalog).
- **C1 ticket (user decisions)**: the HubSpot ticket is optional and never warned about when blank. `service.hubspot_ticket_id`
  turns a pasted HubSpot ticket link (.../ticket/<id>, .../record/0-5/<id>) into the ID (also for a calendar `Ticket:` line,
  `calendar_feed._ticket`); '#' is stripped. `stops.ticket_action` (continue | new | unsure) from the sheet's "This visit is"
  buttons; payload `ticket_action_default` = continue when the last finished visit to the same place ended return/waiting.
  Report `ticket_action` on stops and records_to_update: use (ticket given) | continue | create | find (default). Warning only
  when a typed ticket is not digits. PDF "HubSpot ticket" row explains a blank one.
- **C2 signature**: `REPORT_SIGNING_KEY` (secret, Settings → Email). With it, the report email body has
  `Signature: hmac-sha256=<hex> (of the attached JSON file)` = HMAC-SHA256 of the exact attached JSON bytes (`mailer.signature`).
- **B4-B6**: mileage legs add `to_stop_uid` (None for Home; `to_id` kept). Each `drive_from_previous_stop` adds `miles_method`
  (gps | routed | estimate: how the MILES were found) and `minutes_estimated` (= `estimated`: the MINUTES are a distance
  guess because the phone sent no points). The End Day entry in `excluded` has an empty departure.
- C3 (lunch default): not changed; the user keeps Lunch is paid = on until management decides.
- Unit **stick** (aliases sticks, length, lengths) for things sold by the foot but bought in lengths: unit ft, pack_size 6,
  pack_unit stick (raceway). "+ Add stick (6)" with Paid records the cost per ft.

## Logging (2026-11-23)
- `app/logs.py`: the `jobhub.*` loggers write to stdout (the container log) and a 1000-line memory ring. Every line passes
  `logs.scrub` (numbers with 4+ decimals -> [coord]), tracebacks included. `LOG_LEVEL` / Settings → General → Log detail
  (info | debug), applied on save. Requests: `jobhub.http` "GET /path 200 12ms" — PATH ONLY, never the query string; no static,
  health or manifest. Unhandled errors: `@app.errorhandler(Exception)` logs the traceback with an 8-character code and returns
  "Something went wrong (error <code>)…" (JSON for /api and non-GET). Events (counts only, no names or places): start-up summary,
  sync (`jobhub.sync`), alerts sent, report emails (subject = dates), calendar invites, backups, parts/places imports, receipt
  reads, AI polish. `GET /settings/log[?n=&download=1]` (text) behind the login; Settings → General → Log has View / Download.

## Add stops from receipts (Truck → Receipts, 2026-11-23)
- For catching up past gas and vendor stops (the user has no GPS for those days). `app/receipt_inbox.py`, table
  `receipt_inbox` (path under photos/inbox/, read_json, status new|done|skipped, stop_id, applied_json). Upload one photo per
  request (`POST /api/receipts/inbox`), always read with the Claude reader (needs ANTHROPIC key; counts against the receipt cap);
  the reader now also returns `time` (HH:MM). `GET /api/receipts/inbox` items carry a `proposal` (gas when fuel; arrive/depart from
  the receipt time: gas -8/+2 min, vendor -20/+2; candidates = that day's untyped/gas/vendor stops within -15/+45 min, one = picked)
  and `items` with a guessed catalog part (sku / vendor_item / similar name), a default action (stock if matched, else expense) and
  `packs` (price nearer our cost x pack size: the receipt counts sticks/boxes, converted to stock units on apply).
- `POST /api/receipts/inbox/<id>/apply`: validates everything first, then makes a manual stop (or types an existing one), sets
  `stops.from_receipt`, Gas → fuel (total, gallons, odometer); Vendor → per item: stock (`parts.adjust` with paid, dated the receipt
  day via `ts`), job (purchase logged, tracked parts stocked then used; billable part on the chosen job stop, `/api/receipts/jobs`
  = job stops within 60 days), expense ("Store purchase" expense line, item in `notes`), skip; tax as an expense line (option);
  no items → whole total as one expense (option); vendor stops are finished (Confirmed). The receipt is attached to the stop
  (read_json dismissed). Skip / Delete / Read again. Report: `added_from_receipt` on stops and fuel; expenses carry `notes`
  (PDF and records_to_update show them); PDF stop block "Source: Added from a receipt".
- **Page logging** (`static/clientlog.js`, loaded in `_head.html`; `<meta name="jobhub-log">` = the log detail): page errors,
  unhandled promise errors and failed requests (day.js `api`, `run`) are always POSTed to `/api/client-log` (logger
  `jobhub.page`, path without query, 300 chars, 40 lines/min, scrubbed); with Log detail = debug the part search / Add steps too.
- **Part search** (day.js and truck.js `liveSearch`): picks on pointerup (finger-up, not if the finger moved 12+ px), click as a
  fallback, once; the list stays while touched and hides 400 ms after blur. Ranked: name starts with the text, then all words in
  the name, then anywhere (sku, vendor_item, qbo_item, shelf, bin); 12 shown in a scrolling list with "n more matches".
- After a finger-up pick, `swallowNextClick()` eats the browser's following click (the list is gone, so it would land on a
  link or ✕ underneath and navigate mid-request: Firefox "NetworkError when attempting to fetch resource"). Static files are
  served with `SEND_FILE_MAX_AGE_DEFAULT=0` (no-cache) so phones and desktops use updated scripts at once. Tested with
  Playwright (Firefox mouse + touch, iPhone 13 WebKit) using the container: add by search, quantities, no navigation.
- **Stuck-request watchdog** (`logs.request_started/ended` in before/teardown_request; `logs.start_watchdog` thread with the
  background threads): a request running > 15 s is logged as "request still running after Ns: METHOD /path (n in progress)"
  with the last 8 stack frames, repeated every 30 s while it lasts. Requests slower than 5 s log at WARNING. Gunicorn runs 8
  threads (was 4). Investigated 2026-11-23: the site stopped answering for ~5 min on desktop Firefox with no restart, no OOM,
  background sync still running, and no request finishing; idle pre-connected sockets were tested and do NOT block gthread.
- **Freeze detector** (`logs.start_watchdog`): the watchdog loop re-arms `faulthandler.dump_traceback_later(FREEZE_S=20)` every 5 s;
  if the whole process stops running Python for 20 s, faulthandler's native thread dumps every thread's stack to stderr (container
  log). `faulthandler.register(SIGUSR1, chain=True)`: `docker kill --signal=USR1 JobHub` dumps on demand. Evidence 2026-11-23
  14:52-14:55: DELETE /api/stop-parts/24 logged 200 in 58 ms, but the client got no answer for ~3 min and the phone's /api/live
  polls stopped too: the whole worker stopped answering after the response was built (outside the request watchdog's view).
- **Delivery log**: gunicorn `--access-logfile -` with format `sent %(h)s %(m)s %(U)s %(s)s %(B)sB %(D)sus` (path only, `%(U)s`, never
  the query string) is written only after a reply has been fully sent; the app's `jobhub.http` line (now with the reply size) is
  written when the reply is built. A request with a `jobhub.http` line but no `sent` line was not delivered by the server.
  Findings 2026-11-23 15:13-15:16: the server answered /health every 5 s (port and inside the container) while Firefox showed every
  request "blocked" ~60 s with status 0 (all 6 connections to the host busy waiting on earlier replies that never arrived).
- **Root cause of the "site down for minutes" (2026-11-23)**: not JobHub. Right after an HTTP DELETE from the PC (192.168.5.2), the PC
  could not reach the server (192.168.128.245, another subnet, so through the router) even with new connections (PowerShell probe
  `000` from 15:24:06) while the server answered on Unraid itself; the browser capture shows the first DELETE answered and the next
  request never connecting. Signature of a router intrusion-prevention rule blocking the sender after a DELETE. Fix in JobHub: pages
  send DELETE and PUT as POST + `X-Method-Override` (`wireMethod`/`overrideHeader` in day.js, truck.js, places.js, tools.js, home.js);
  a WSGI wrapper in create_app turns POST + X-Method-Override (DELETE/PUT/PATCH) back into that method. Keep it that way.

## Bought here (vendor stops) and deleting parts (2026-11-23)
- A vendor stop's sheet shows **Bought here** instead of Parts used (`boughtBlock`, day.js): search a part (a pick fills the
  box; "Count is in sticks of 6" tick for pack items, on by default for sticks), qty, "$ paid" (line total, optional), Add; a
  new name opens a small form (counted in, type, part #, price you charge). Rows: − qty +, paid, ✕ (confirm). Purchases ADD
  to truck stock (part becomes tracked and active again), are logged in `purchases` with `stop_id` (migration), vendor = the
  stop's name and ts = the stop's arrival (`parts.buy_at_stop`); last_cost follows the newest priced purchase. Never billed.
  `POST /api/stops/<id>/bought`, `PATCH|DELETE /api/purchases/<id>`. Old part lines on a vendor stop (they took stock OFF)
  show "Move to Bought here" (`POST /api/stop-parts/<id>/to-bought`). Receipt items on a vendor stop: "+ Add as bought".
  Truck → Receipts (inbox) stock/job lines link their purchase to the stop too. Purchases move with merge, re-attach on
  re-detect like photos, stay in stock when the stop is deleted, and Undo puts their stop link back (snapshot `purchases`).
- Report (schema "1", additive): per stop `bought_here` [{name, part_number, quantity, unit, paid}], `summary.bought_total`,
  records_to_update `bought_here` strings; omit=costs drops `paid` and `bought_total`. PDF: "Bought here" table (Parts used
  row hidden on a stop with purchases and no parts), top-section "Bought for truck stock". Agent README explains it.
- **Delete permanently** (part page, only when never on a stop; `DELETE /api/parts/<id>`, `parts.purge_part`): refused when on a
  stop line or in a kit; deletes its inventory_log and purchases.
- `swallowNextClick(rect)` (day.js, truck.js) now eats only a click inside where the pick list was, so a quick tap on Add
  right after picking is not lost.

## Running late (built 2026-11-23)
- `app/late.py`. Settings → Alerts: **Running late alert** (`ALERT_LATE` on|off, default on) and **alert when late by**
  (`ALERT_LATE_MIN`, default 10); Settings → Email: **Send late notices to** (`LATE_NOTICE_TO`, blank = MAIL_TO).
- Alert: the alert thread (`alerts.late_conditions`, during the work day until End day, Pushover configured, a calendar
  link set) every 3 min (`CHECK_EVERY_S`) takes the phone's position from the last hour of points (fresh within 15 min),
  the next upcoming on-site appointment starting within 3 h, and `summary.eta` (shared with the Home page). Late by
  ALERT_LATE_MIN or more → one Pushover per appointment (`late:<event uid>`, once): "You may be about 15 min late for
  your 10:00 appointment (arriving about 10:15)" — no names, places or tickets. It opens `/?late=1` (APP_URL).
- Home page: the Next appointment card shows a red box when late (`next.late`, `next.uid`, `eta.late_min`, `eta.arrive_ts`),
  an optional note and **Tell the customer** (confirm) → `POST /api/late-notice {uid, note}`: recomputes the ETA from the
  live position (refuses when on time, already there, or the appointment is gone) and emails `JobHub Late Notice
  <ticket|customer|time>`: body lines + `jobhub-late-notice-<date>-<hhmm>.json` (`late.notice`: technician, hubspot_ticket,
  qbo_estimate, customer, address, contact, appointment_start/end, new_eta, minutes_late, eta_method, note,
  calendar_event_uid), HMAC signature line with REPORT_SIGNING_KEY. Logged in `late_notices` (NOT email_log, which numbers
  report versions); `next.late_notice` {time, minutes_late, mail_to} shows "✓ Late notice sent" and the button becomes
  "Send an updated late notice". The page skips its minute refresh while the late box has focus.
- Agent docs: `docs/sample-report/README.md` "Late notices", `late-notice-sample.json`, `late-notice-body.txt`.
- Calendar matching fix: a stop that ended before an appointment began only matches it when it has the appointment's
  name (the job before it no longer "uses up" the next appointment, which hid it from Home and the late alert).
