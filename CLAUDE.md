# JobHub (repo: stop-journal): personal job-site and travel logger

A self-hosted web app for one field technician (the owner). It turns OwnTracks location data into a daily log of stops
and drives. The owner fixes each day on the phone and emails reports to a separate **work AI agent**, which updates
HubSpot and QuickBooks. JobHub never calls HubSpot or QuickBooks itself; the agent is the bridge, by email.

**How each feature works, and why, is in `docs/FEATURES.md`.** Read the relevant section before changing a feature,
and add or update a section there when you build something. This file holds only the rules, the code map and the plan.

## The user and the setup
- One user ("Greg"), testing on an iPhone Home Screen app and on desktop Firefox on Windows. Both home and office use
  UniFi routers.
- The app runs in Docker on Unraid as the container "JobHub": gunicorn, 1 worker, 8 threads, port 8000, server
  192.168.128.245. Remote access: Cloudflare tunnel + Cloudflare Access + JobHub login (built 2026-11-25); the VPN
  stays as a backup. OwnTracks goes through the tunnel to NPM (basic auth); JobHub reads the Recorder on the LAN.
- The user deploys with `git pull`, rebuilds and restarts the container, reloads the browser and reopens the phone app.
  So when a change is done and tested: commit and push it (on main), then tell the user it needs that update.
- `docs/SETUP-CHECKLIST.md` is the user's setup and test list; `docs/sample-report/` is what the agent was given
  (regenerate it with `python scripts/make_sample_report.py` after any change to the report, the invoice request or the
  late notice).

## Working agreements
- Ask the user before choosing Google Places over OSM, and before anything that costs money.
- Keep dependencies few and pinned (`requirements.txt`; the dev venv must match it: `pip install -r requirements.txt -r
  requirements-dev.txt`). Keep the README current (features, settings table, security section).
- Never hardcode credentials or the user's addresses in the repo. User data (their parts CSV, real days, logs) stays
  out of the repo: use the scratchpad.
- Settings are environment variables with Settings-page overrides (`app/config.py`, `app/settings.py` SPEC/TABS).
  Secrets are write-only (`settings.SECRETS`).
- Agent JSON comes in two formats: "1" (report, invoice request, late notice) and "2" (`app/message_v2.py`, the agent's
  spec, chosen by Settings → Email → Message format). Within a format only ever ADD fields, never rename or remove
  (the agent depends on them). Format 1 is removed only after the user confirms the agent runs on 2.
- The database column for "Work performed" is `stops.notes`; user-facing text and the JSON key say Work performed /
  `work_performed`.

## Privacy and security (hard rules)
- Location data stays on the server. Outbound calls are only: place lookups (coordinates and search text to the chosen
  provider, cached), map tiles, road routing (coordinates rounded to ~100 m), the published calendar link, the
  optional Google Vision / Claude / LanguageTool calls (only the receipt photo or the note text), Pushover, Web Push
  (to the push service of each phone/browser the user turned on: Apple, Google, Mozilla, Microsoft only), and Gmail.
- Notification text (Pushover and Web Push alike, all through `notify.send`) must never contain coordinates,
  addresses, business names or ticket numbers: it passes through a third party. Use generic text ("your 10:00 appointment", "the stop you arrived at 9:12").
- Emails to the user's work address (all user-approved 2026-11-23), never sent automatically except invites:
  - Email report (full report): only when the user taps Email report.
  - Send for invoice (one stop) and Tell the customer (late notice): only on that tap, after a confirm.
  - Calendar invites (when turned on): each stop's type, customer, address, ticket, times. Never notes, parts or prices.
- Logs: path only, never the query string; every line passes `logs.scrub` (numbers with 4+ decimals are blanked).
  Never log coordinates, customer names or places. No analytics or telemetry.
- Public access is a Cloudflare tunnel with Cloudflare Access in front AND JobHub's own login (`app/auth.py`). Every new
  route needs the login (only `OPEN_ENDPOINTS` skip it); new changes must be POST from our own pages (Origin check).
  Never add a Cloudflare bypass for JobHub paths. Password reset emails go only to the account's own email.
- Technicians other than the owner would need written consent and work-hours-only tracking: do not build it.

## Hard-won gotchas (keep these)
- **No HTTP DELETE or PUT on the wire.** The user's UniFi intrusion prevention blocks the sending device for minutes
  after a DELETE (found 2026-11-23). Pages send DELETE/PUT as POST + `X-Method-Override` (`wireMethod`/`overrideHeader`
  in each page script); a WSGI wrapper in `create_app` restores the method. Any new page or fetch must do the same.
- **iPhone Home Screen app**: iOS shows only page 1 of a PDF and window.open stays in the app, so PDFs open in
  `/api/export.view` (pages rendered by `pypdfium2`). Web Share (Share / Save to Files) works only on https (the tunnel
  address); on the http home address the fallback is "Copy link for Safari" (10-minute token). share.js picks by
  whether `navigator.canShare` exists.
- **Part search** picks on pointerup (finger-up), then `swallowNextClick(rect)` eats the browser's following click only
  inside where the list was (else it lands on a link underneath, or eats a quick tap on Add).
- Static files are served with no-cache (`SEND_FILE_MAX_AGE_DEFAULT=0`) so phones pick up new scripts at once.
- `.btn` is a LINK style: `.btn:not([href])` has pointer-events off. Use `<button class="primary">` for buttons.
- New per-stop tables must be handled by merge, re-detect (re-attach by the old stop's middle time, like photos and
  purchases), delete and Undo (`service.snapshot_day` / `undo_last`).
- `email_log` counts report versions ("(update n)"): log other emails in their own tables (late_notices,
  invoice_requests).
- The Settings form and pages are plain same-origin POSTs; a "came from another website" 403 means the Origin host is
  not the request host / X-Forwarded-Host / App address (check the tunnel's Host header and Settings → App address).
- Calendar matching: a stop that ended before an appointment began only matches it if it has the appointment's name.
- Don't `pkill -f`/`pgrep -f` with a pattern that also matches your own shell command (it kills that shell): stop a
  test server in a separate command from the one that started it, with a pattern like `"[g]unicorn --bind 127..."`.

## Code map
- `app/__init__.py`: Flask app factory and every route (`with_conn`, `mutate` = run + return the day payload,
  `catalog` = run + JSON); method override wrapper; request logging; error handler with a reference code.
- `app/service.py`: the day (detect/sync/apply places, stop edits, finish, merge/split/undo, drives and time rules,
  calendar matching, day payload). `app/detect.py`, `points.py`, `recorder.py`, `live.py`: GPS to stops.
- `app/report.py` (JSON report format 1, warnings, omit; `build_day(keep_src=True)` feeds format 2),
  `app/message_v2.py` (format 2: envelope, Customer/Job/Stop/Line/Photo, all three messages), `app/address.py` (US
  address split/join), `app/version.py`, `app/pdf.py` (ReportLab PDFs), `app/pdfview.py` (PDF pages as images),
  `app/mailer.py` (Gmail, report email, signature), `app/invoice_request.py`, `app/late.py`, `app/calendar_out.py`
  (invites), `app/calendar_feed.py` (Outlook ICS in), `app/summary.py` (Home page, shared ETA `summary.eta`).
- `app/parts.py` (catalog, stock, purchases, Bought here, CSV and QuickBooks import), `kits.py`, `vendors.py`,
  `equipment.py` (tools), `receipts.py` + `ai.py` (receipt reading, Polish notes), `receipt_inbox.py` (Truck → Receipts),
  `photos.py`.
- `app/places/` (known sites, OSM/Google providers, ranking, cache), `routing.py` (OSRM), `grammar.py` (LanguageTool).
- `app/auth.py`: accounts, sign-in throttle, password reset tokens, console (`python -m app.auth`).
- `app/alerts.py` (background thread, every 60 s: alerts, invites, weekly `housekeeping.maybe_prune`),
  `notify.py` (sends to iPhone and/or Pushover), `webpush.py` (native notifications: keys, devices, sending;
  `static/sw.js` shows them, `static/account.js` turns them on), `logs.py` (logging, watchdog, freeze detector), `backup.py`, `db.py` (schema + MIGRATIONS).
- Pages: `app/templates/*.html` and `app/static/{day,truck,places,tools,home}.js` (vanilla JS, each with its own copy of
  the helpers: see plan E), `clientlog.js` (page errors to `/api/client-log`), `share.js`.

## Testing
- `pytest -q` (about 350 tests; fixture days in `tests/`; `scripts/make_sample_report.py` builds the full sample day
  used by several tests).
- Browser checks: run the app with gunicorn on 127.0.0.1 (DATA_DIR in the scratchpad) and Playwright from
  `podman run --rm --network host mcr.microsoft.com/playwright/python:v1.49.0-noble` (`pip install playwright==1.49.0`
  inside), Firefox desktop and the iPhone 13 WebKit profile. Do this for any change to page scripts.

## Action plan (updated 2026-11-23)
QuickBooks makes estimates and invoices; a HubSpot service ticket holds the job. Match on the HubSpot ticket number
(calendar `Ticket:` line, then the stop's ticket); the QuickBooks estimate number is info.

A. User status: deployed through the router fix; test invite works; Anthropic key loaded; the agent ran 4 real days.
   Testing next: calendar invites over a work day, Outlook appointments, the phone app, Bought here, running late, Send
   for invoice (checklist 7, 7b, 7c). User to do: parts clean-up, HubSpot company IDs CSV, QuickBooks parts import.

B. Built 2026-11-24: message format 2 (agent's spec; setting, test send, samples in docs/sample-report/v2/), stable
   uids on lines/follow-ups/photos/purchases, address parts + QuickBooks customer ID + billing email on places,
   Waiting split into parts/customer, billing email on Send for invoice. Next: user tests (checklist 7d), agent checks
   a real format 2 day, user switches the setting; remove format 1 a week later when the user says so.
   Built 2026-11-23: Bought here, delete parts permanently, running late, Send for invoice (Time & Material only;
   labor documented as minutes, the agent prices it and handles invoice, payment and trip charges), clean-up
   (dead code, README, this file split, weekly trim).

C. Next, when the user is ready (they will review first):
   1. Estimates: sample estimate email for the agent (`JobHub Estimate <ticket>`: ticket, QuickBooks estimate #,
      customer, quoted hours, quoted parts); JobHub reads the Gmail inbox (IMAP, same App password, that subject and
      allowed senders only) and stores it per ticket; stop sheet Estimate box with **Add quoted parts** (never
      automatic); report quoted vs actual per job (all visits with the same ticket).
   2. The agent's reply to an invoice request ("Invoiced #1234, $480") shown on the stop (needs the same Gmail reading).
   3. Send for invoice for quoted projects (from the QuickBooks estimate), if the user wants it.
   4. The offline app shell (https now comes from the tunnel).
   5. Walk-through estimate stop type (planned 2026-11-25, on hold by the user): details box, proposed parts kept out of
      stock, floor plan file, a format 2 "JobHub Estimate Request" so the agent drafts the QuickBooks estimate. Open
      questions: which floor plan app, eighth work category or not.

D. Suggested, not chosen: weekly/monthly summary; yearly mileage log and year-end package; arrival prompt; missing-receipt
   reminders; security check before any outside access. Parked: reading HubSpot directly (read-only token), Microsoft
   Graph instead of invites (IT approval), Auris / QuickBooks time cross-check, off-site photo backup (costs money:
   ask), cleanup of photos from deleted stops (the user keeps them).

E. Refactor for a quiet day (no behaviour change; test every page in Firefox and iPhone WebKit afterwards):
   1. One shared `app/static/common.js` for the helpers each page script copies today: `el`, `api`/`post` with the
      DELETE/PUT override, `liveSearch` + `rankParts` + `partLabel`, `swallowNextClick`, `jl` logging. Load it in
      `_head.html` before the page script. Removes 5 copies of the router workaround.
   2. Split the biggest files by area: `app/__init__.py` (~70 KB of routes) into Flask blueprints (day, parts/truck,
      places, reports/email, settings); `app/service.py` (~90 KB) into day/time rules, stop edits, calendar matching;
      `app/static/day.js` (~110 KB) into the stop sheet, the card and the day list.
