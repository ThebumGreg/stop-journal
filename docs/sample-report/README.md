# JobHub sample report (for setting up the work AI agent)

A made-up day (all names, addresses and coordinates are invented) that uses every field, made by
`python scripts/make_sample_report.py`. Have the agent read these and propose how it will update HubSpot
**before** it writes anything real.

| File | What it is |
|---|---|
| `jobhub-daily-summary-2026-11-18.eml` | The email exactly as **Email report** sends it (open it in a mail app). Attachments: JSON, PDF, 2 pictures. |
| `email-body.txt` | The subject and body of that email as plain text. |
| `jobhub-daily-summary-2026-11-18.json` | The data. **The agent should use this, not the PDF.** |
| `jobhub-daily-summary-2026-11-18.pdf` | The same report for people. |

## The day in the sample
1. Home (Personal) → Start day 07:38.
2. **Sample Dental Office** (HubSpot company 18200000001): Time & Material, HubSpot ticket 24681357913 (pasted as a link), Complete; 140 ft Cat 6 cable, 4 keystone jacks (billable parts),
   zip ties (non-billable material), $12 parking (expense) with its receipt, one photo.
3. Fill-up at Sample Fuel Station (counted as drive time; fuel table).
4. **Sample Supply House** (vendor stop: Internal operations, not billable).
5. Lunch at Sample Deli (paid, not billed; listed by time only).
6. **Sample Medical Clinic** (HubSpot company 18200000002): Quoted project, no ticket given ("Continuing a job": `ticket_action` continue), QuickBooks estimate 2210,
   Return visit needed; parts; two follow-ups (Office → office@work.example: send a quote, due 11-20; Me →
   tech@work.example: return visit, due 11-24).
7. End at home.

## Rules for the agent
- Find emails whose subject starts with `JobHub Daily Summary`. A subject ending `(update N)` replaces the earlier email
  for the same dates: update what was written from the earlier one, do not add it twice.
- `schema_version` is "1". New fields may be added later; ignore fields you don't know.
- Only stops in `days[].stops` (confirmed) are work to record. `days[].excluded` is listed by time only on purpose
  (Personal, Lunch, fuel stops, End Day) and must not create records.
- `records_to_update` is the one-row-per-stop list to act on: business name, address, arrival, departure, category,
  job reference, work performed, parts used.
- **Identify each stop by `stop_uid`**, never by `stop_number` (numbers shift when stops are added, merged or split).
  On an `(update N)` email: update what you wrote for each `stop_uid`; a `stop_uid` that is no longer in the update was
  merged or deleted, so remove or mark what you created for it. Follow-ups carry `follow_up_id` for the same purpose.
  `days[].report_key` is the date the day belongs to.
- `technician` {name, email} is who did the work: use it as the owner of notes.
- **The HubSpot ticket is optional.** Use `ticket_action` on each stop / record:
  - `use`: `hubspot_ticket` holds the ticket ID: update that ticket.
  - `continue`: the technician says this continues an open job: find the open ticket for this company; if there are
    several, ask instead of guessing.
  - `create`: a new job: create a ticket.
  - `find`: not said: if the company has exactly one open ticket use it, otherwise create one and flag it for the office.
  Keep your own record of which `stop_uid` went to which ticket, so an `(update N)` email updates the same ticket.
- `records_to_update[]` has everything needed per stop (billing, visit_result, time_on_site_minutes, technician_email,
  parts/consumables/expenses as text, photo_attachments, ticket_action). `photo_attachments` and `days[].photos[].attachment`
  are the exact attachment file names in the email.
- Billing: `summary.billable_on_site_minutes` leaves out non-billable stops; `on_site_minutes` is all on-site time.
  "Ready for invoicing" only when `visit_result` is Complete AND `billing` is not `non_billable`.
- Drives: `miles_method` says how the MILES were found (gps = recorded drive, routed = road route, estimate = straight
  line x1.25); `minutes_estimated` says the MINUTES are a guess because the phone sent no location for part of the trip.
  They measure different things, so one can be an estimate while the other is not. Mileage legs link to stops by
  `to_stop_uid` (null = Home).
- **Signature** (when the office set a signing key): the email body has `Signature: hmac-sha256=<hex>`. Compute
  HMAC-SHA256 of the attached JSON file's bytes, exactly as received, with the shared key; reject the email if it differs.
  Also accept reports only from the JobHub Gmail address.
- `hubspot_company_id`: use it when set. When blank, match the company by business name and address, and say so.
- `hubspot_ticket` is the HubSpot service ticket number (digits); `qbo_estimate` is the QuickBooks estimate number
  (quoted projects and migrations). `job_reference` is the raw text the technician typed (kept for compatibility).
- Follow-ups have `owner` and `owner_email`: assign the HubSpot task to the user with that email; a blank email means
  the owner is unknown, so ask instead of guessing.
- Per stop: `work_performed` (the note), `category` + `billing` (hourly | project | non_billable), `job_reference`
  (ticket), `visit_result`, `time_on_site_minutes`, `parts_used` (billable flag, qty, unit, unit cost),
  `consumables_used` (never billed), `expenses`, `follow_ups`, `photos`.
- `bought_here` (vendor stops): parts bought for the truck's stock (name, part_number, quantity, unit, paid). They are
  NOT used on a job and NOT billable: never put them on a customer, ticket or invoice. A part is billed only when it
  later appears in a job stop's `parts_used`. `summary.bought_total` is the day's total paid; `records_to_update[]`
  has them as text. Without costs (`omitted` costs) `paid` is left out.
- Top-level `follow_ups` (owner, due) are natural HubSpot tasks.
- `omitted` lists sections left out on purpose (for example GPS coordinates or costs); they are not missing data.
- Times are local, in the report's `timezone`.
- Time totals: `paid_minutes` = on-site + `drive_minutes` + lunch when `lunch_paid` is true. `drive_minutes` includes
  `traffic_minutes` (time at fuel stops, short breaks and traffic: `fuel_stop_minutes` and `short_break_minutes` are part
  of it). The `timesheet` rows with `paid: true` add up to `paid_minutes`.
