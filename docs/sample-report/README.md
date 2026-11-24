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

## Message format 2 (`v2/`)
Built to the agent's spec "JobHub message format, schema version 2" (with its section 8 answers and the sample review
of 2026-11-24). JobHub sends format 1 until **Settings → Email → Message format** is set to 2; **Email report →
"Send as format 2 (test)"** sends one day in format 2 first, for the agent to check. Format 2 samples (the same
made-up day, with the clinic visit booked as a calendar appointment and real-looking QuickBooks SKUs):

| File | What it is |
|---|---|
| `v2/invoice-request-sample.json` + `-body.txt` | Invoice request for the Time & Material stop, **sent while still on site** at 10:22 (`still_on_site` true, 142 min). The parking is billed, so its receipt is attached. |
| `v2/daily-summary-sample.json` + `-body.txt` | Daily summary, revision 1, sent after the day. The dental stop now ends 10:30:30 (150 min) and carries `invoice_request` (10:22, 142 min): the difference to flag. JSON, PDF and 2 pictures attached. |
| `v2/daily-summary-r2-sample.json` + `-body.txt` | The same day, revision 2 ("(update 2)", `supersedes` = revision 1): the supply house stop was deleted, the clinic's cable changed 60 → 80 ft (same `line_uid`) and its jacks were removed. |
| `v2/late-notice-sample.json` + `-body.txt` | Late notice for the 12:00 clinic appointment; its `calendar_event_uid` and ticket match the clinic stop in the summary. |

### Rules for the agent (format 2)
- Act on `days[].stops[]` (confirmed stops only). Find earlier records by `stop_uid`, `line_uid`, `follow_up_uid`,
  `photo_uid`; never by stop numbers or names.
- The highest `revision` for a `subject_key` wins. A revision > 1 replaces the earlier one: update what you wrote,
  never add it twice. Each day also lists what is gone since the previous send of the same dates:
  - `removed_stops` [{`stop_uid`, `merged_into`}]: `merged_into` null = the stop was deleted or is no longer work
    (e.g. marked Personal): remove what you wrote for it. `merged_into` = a `stop_uid`: it was joined with that stop:
    move its records there, do not delete them.
  - `removed_line_uids`, `removed_follow_up_uids`: lines and follow-ups deleted (a changed quantity keeps its
    `line_uid` and is not listed). Items of a section left out on purpose (`omitted`) are never listed as removed.
  - A removed stop that was already invoiced (it had `invoice_request`): do not void the invoice; tell the technician.
- An invoice request with `revision` > 1 also has `removed_line_uids` (lines on the earlier request that are gone).
  Update the invoice only while it is unsent; otherwise flag the difference.
- `stop.invoice_request` set in a daily summary: never invoice that stop again; if its minutes or lines differ from
  the request (`billed_through`, `time_on_site_minutes`), flag it to the technician.
- Prices on the invoice come from QuickBooks (`sku`, then `qbo_item_name`, then `name`). `unit_cost` is our purchase
  cost: documentation only.
- Late notice: the company has not decided who contacts the customer; draft the message for the technician or the
  office to send.

### As built, in the spec's terms
- **Envelope** on every JSON: as specified. `subject_key` is the date (a range: `2026-11-19_2026-11-23`), the
  `stop_uid` (invoice request) or the `calendar_event_uid` (late notice). `revision` counts successful sends of that
  key; the subject's "(update N)" is N = `revision`. `supersedes` is the previous send's `message_id` (`null` when that
  send was format 1 or there was none). `app.version` starts at 2.0.0. Every format 2 body has the same three lines
  `Message format: 2`, `Revision: N`, `Message ID: …`; `Signature:` is over the JSON bytes as before. The body names
  only what is attached.
- **Attachment names**: `jobhub-daily-summary-<date>-r<rev>.json` / `.pdf`,
  `jobhub-invoice-request-<date>-stop-<n>-<customer>-r<rev>.json`, `jobhub-late-notice-<date>-<HHMM>-r<rev>.json`;
  pictures `photo-<photo_uid>.jpg`. Each Photo has `attached` (true when its picture is attached to this email) and
  `taken_at` (the phone's capture time; "" when the picture has none, e.g. pictures added before 2026-11-24).
- **Customer**: `service_address` comes from the saved place (the technician can type street, suite, city, state, ZIP
  on Places), else the stop's one-line address is split; `formatted` is always the full line. `latitude`/`longitude`
  are the stop's position (`null` when coordinates are left out). `site_contact` is the calendar appointment's contact.
- **Job**: `hubspot_ticket_id` is a real ID (all digits, 9+ long; links are reduced to the ID): the one typed on the
  stop, else the one on the matched calendar appointment (the same value its late notice sends). A short number goes
  to `tech_typed_reference` with `hubspot_ticket_id` "" and `ticket_action` from the technician's choice (default
  `find`).
- **Stop**: addition `calendar_event_uid` (the matched appointment, "" if none; joins a late notice to its visit).
  JobHub keeps the match on the stop, so later revisions have it even after the published calendar drops the day.
  `visit_result`: `complete`, `return_visit`, `waiting_parts`, `waiting_customer` (answers from before the split are
  `waiting_parts`), "" when not set. `breaks` lists lunch (`paid` per the lunch setting) and personal breaks (unpaid)
  taken in the middle of one visit; never billable.
- **Line**: `unit_price`/`amount` are the price charged; `unit_cost` is what JobHub knows we pay per unit (the parts
  list's purchase cost or the last price paid; for an expense, the expense itself). Documentation only.
  `consumable` and `stock` lines are never charged: `unit_price` and `amount` are "", what we paid is in `unit_cost`
  (a consumable never bought through JobHub shows its catalog price there). Additions: `qbo_item_name` (the QuickBooks
  product/service name from the QuickBooks parts import; `qbo_item_id` is "" because that export has no item ID), and
  on `stock` lines `amount_paid` (the total paid for the line). `receipt_photo_uid` is filled for expense and stock
  lines when the stop has exactly one receipt picture.
- **Daily summary**: `records_to_update` and top-level `follow_ups` are gone. Additions per day: `removed_stops`,
  `removed_line_uids`, `removed_follow_up_uids` (above), `totals.stops_confirmed` / `stops_excluded`, `end_day.time` /
  `miles_to_home`, and `fuel[].stop_uid` (the ID of the fuel stop, which is not in `stops`: fill-ups count as drive
  time, so the stop is listed under `excluded`). `excluded[].end` is "" for the End Day stop. Omitted sections are
  empty (`[]`, `""`, `null`), never missing; `omitted` lists the keys `costs`, `expenses`, `fuel`, `photos`,
  `materials`, `categories`, `follow_ups`, `mileage`, `recap`, `coordinates`.
- **Invoice request**: `stop` is the same Stop object as in the daily summary. When sent while still on site
  (`still_on_site` true), the Stop's `departure` and `time_on_site_minutes` run to `billed_through` (the moment of
  sending, to the second; the summary's `invoice_request.billed_through` is the same value). Receipts are always
  attached when an expense is billed; other pictures only when the technician ticks Photos. Additions:
  `invoice_to_email` (the billing email the customer gave on site, "" if none), `photos` (Photo objects of the stop)
  and `removed_line_uids`. The body lists parts as "Parts (JobHub prices; the invoice uses QuickBooks prices)".
- **Late notice**: as specified. `stop_uid` is "" (the visit does not exist yet when it is sent). The customer's
  company ID and address parts come from a saved client place with the appointment's name and street; otherwise only
  the calendar's text (`formatted`, the rest ""). The body no longer asks the agent to contact the customer.

## The day in the sample
1. Home (Personal) → Start day 07:38.
2. **Sample Dental Office**, Suite 210 (HubSpot company 18200000001, QuickBooks customer 58): Time & Material, HubSpot ticket 24681357913 (pasted as a link), Complete; 140 ft Cat 6 cable, 4 keystone jacks (billable parts),
   zip ties (non-billable material), $12 parking (expense) with its receipt, one photo.
3. Fill-up at Sample Fuel Station (counted as drive time; fuel table).
4. **Sample Supply House** (vendor stop: Internal operations, not billable).
5. Lunch at Sample Deli (paid, not billed; listed by time only).
6. **Sample Medical Clinic** (HubSpot company 18200000002): Quoted project, no ticket given ("Continuing a job": `ticket_action` continue), QuickBooks estimate 2210,
   Return visit needed; parts; two follow-ups (Office → office@work.example: send a quote, due 11-20; Me →
   tech@work.example: return visit, due 11-24).
7. End at home.

## Rules for the agent (format 1)
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

## Late notices, format 1 (`JobHub Late Notice <ticket>`)

A second kind of email, sent only when the technician taps **Tell the customer** because they will be late for an
on-site appointment. Samples: `late-notice-body.txt` (the email body) and `late-notice-sample.json` (the attachment,
`jobhub-late-notice-<date>-<hhmm>.json`).

- `type` is `jobhub_late_notice` (`schema_version` "1"). Use it to tell the customer the technician is running late.
- `hubspot_ticket` (may be blank) and `customer` / `address` identify the job; match the HubSpot company by name and
  address when there is no ticket. `contact` is the contact line from the technician's calendar, when there was one;
  prefer the ticket's or company's contact in HubSpot.
- `appointment_start` / `appointment_end`: the booked window. `new_eta`: when the technician now expects to arrive;
  `minutes_late` is the difference. `eta_method` is "road route" (drive time without traffic) or "estimate".
- `note`: the technician's own words for the customer (may be blank). Pass it on politely; do not invent a reason.
- More than one notice can arrive for the same appointment (`calendar_event_uid`): the newest one wins. Tell the
  customer once, then only again if the new time changes by 10 minutes or more.
- Same rules as reports: accept it only from the JobHub Gmail address, and check the `Signature:` line when the office
  set a signing key.

## Invoice requests, format 1 (`JobHub Invoice Request <date> – <customer>`)

Sent only when the technician taps **Send for invoice** on a finished **Time & Material** stop, usually while still
on site, so the customer can pay before the technician leaves. Samples: `invoice-request-body.txt` (the email body),
`invoice-request-sample.json` (the attachment `jobhub-invoice-request-<date>-stop-<n>-<customer>.json`) and
`invoice-request-sample.pdf`.

- `type` is `jobhub_invoice_request` (`schema_version` "1", `invoice_requested: true`). Create the QuickBooks invoice
  for this one visit and handle sending it and collecting payment as the company does.
- Customer: `customer.hubspot_company_id` when set, else match by `business_name` + `address`. Ticket: `hubspot_ticket`
  (blank → follow `ticket_action` as in the daily summary). `qbo_estimate` is informational for T&M.
- **Labor** is documented, not priced: `labor.time_on_site_minutes` (and `hours`) from `arrival` to `billed_through`.
  Lunch and personal breaks are already taken out (listed in `lunch_non_billable` / `personal_breaks_unpaid`).
  `still_on_site: true` means the time ran to the moment of sending. Apply the company's labor rates and rules
  (minimums, increments) yourself. `drive_from_previous_stop` is there in case the company bills travel; JobHub does not.
- **Parts**: `parts` are billable, `unit_cost` = the price charged per unit, `line_cost` = quantity x price,
  `qbo_item` = the QuickBooks item when known. `parts_not_billed` are for the record only. `expenses` are billable
  expenses (`line_cost`); `expenses_not_billed` are not.
- `request_version` > 1 (subject "(update n)") replaces the earlier request for the same `stop_uid`: if that invoice is
  not paid yet, update it; if it is paid, do not change it, tell the technician what differs.
- **The daily summary later lists the same stop again** with `invoice_request` {version, sent_at, billed_through,
  time_on_site_minutes}. Never invoice that stop a second time from the summary. If the summary's minutes or parts
  differ from the invoice request, flag the difference to the technician instead of changing the invoice.
- Same rules as the other emails: only from the JobHub Gmail address, and check the `Signature:` line when the
  office set a signing key.
