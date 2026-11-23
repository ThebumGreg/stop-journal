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
2. **Sample Dental Office**: Time & Material, ticket 1042, Complete; 140 ft Cat 6 cable, 4 keystone jacks (billable parts),
   zip ties (non-billable material), $12 parking (expense) with its receipt, one photo.
3. Fill-up at Sample Fuel Station (counted as drive time; fuel table).
4. **Sample Supply House** (vendor stop: Internal operations, not billable).
5. Lunch at Sample Deli (paid, not billed; listed by time only).
6. **Sample Medical Clinic**: Quoted project, ticket PRJ-2210, Return visit needed; parts; two follow-ups (Office: send a
   quote, due 11-20; Me: return visit, due 11-24).
7. End at home.

## Rules for the agent
- Find emails whose subject starts with `JobHub Daily Summary`. A subject ending `(update N)` replaces the earlier email
  for the same dates: update what was written from the earlier one, do not add it twice.
- `schema_version` is "1". New fields may be added later; ignore fields you don't know.
- Only stops in `days[].stops` (confirmed) are work to record. `days[].excluded` is listed by time only on purpose
  (Personal, Lunch, fuel stops, End Day) and must not create records.
- `records_to_update` is the one-row-per-stop list to act on: business name, address, arrival, departure, category,
  job reference, work performed, parts used.
- `hubspot_company_id` is empty for now: match the company by business name and address.
- Per stop: `work_performed` (the note), `category` + `billing` (hourly | project | non_billable), `job_reference`
  (ticket), `visit_result`, `time_on_site_minutes`, `parts_used` (billable flag, qty, unit, unit cost),
  `consumables_used` (never billed), `expenses`, `follow_ups`, `photos`.
- Top-level `follow_ups` (owner, due) are natural HubSpot tasks.
- `omitted` lists sections left out on purpose (for example GPS coordinates or costs); they are not missing data.
- Times are local, in the report's `timezone`.
