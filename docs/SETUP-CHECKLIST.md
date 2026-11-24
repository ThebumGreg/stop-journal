# JobHub setup and test checklist (updated 2026-11-23 evening)

Work down the list. Items marked **Tell Claude** are results to report back. Ticked items are ones you reported done.

## 1. Update the server
- [x] `git pull`, then `docker compose build` and `docker compose up -d` (repeat for every update).
- [x] On the iPhone, close the Home Screen app fully and open it again (loads the new pages).

## 1b. Check the update worked
- [ ] Settings → General → Log → **View recent log** shows "JobHub started: …".
- [ ] Reports → Download PDF → **View here** shows every page.

## 2. Settings → General
- [ ] **Technician**: your name and work email.
- [ ] **Follow-up owners**: `Me` plus one line per person, e.g. `Office = office@yourco.com`.
- [ ] **Pay rules → Lunch is paid**: leave on unless the company says lunch is unpaid.

## 3. Gmail (reports and calendar invites)
- [x] Create a separate Gmail for JobHub (e.g. `yourname.jobhub@gmail.com`).
- [x] Turn on 2-Step Verification, then create an **App password** named JobHub.
- [x] Settings → Email: Send reports to (your work email), the Gmail address, the App password. Save.
- [x] **Send test email**. In Outlook, add the Gmail address to Safe senders and check junk.
- [x] **Send test calendar invite**. It appeared on its own.
- [ ] Turn on **Put my stops on my work calendar**. Check your calendar during a work day.

## 4. Outlook calendar (appointments into JobHub)
- [ ] Outlook on the web → Settings → Calendar → Shared calendars → Publish a calendar → "Can view all details" → copy the **ICS** link.
- [ ] Settings → Calendar: paste the link → **Test calendar**.
- [ ] Format on-site events like this:
  - Title: `Onsite – Customer name`
  - Location: the full street address
  - First lines of the notes: `Ticket: 1042`, `Estimate: 2210`, `Contact: Jane 555-0100`, `Work: what to do`,
    `Category: Time & Material`

## 5. Receipts and AI
- [ ] **Google Vision**: enable **Cloud Vision API** in the Google Cloud project, make sure billing is on, and allow the
      API on the key. Settings → Receipts → `google_vision`. Test with a receipt photo on a stop.
- [x] **Or Claude**: get an Anthropic API key (console.anthropic.com) and set a monthly spend limit there.
      Settings → AI: paste the key. Try ✨ Polish notes. Optionally Settings → Receipts → `claude`.

## 5b. Parts clean-up
- [ ] Move the stock counts from the parts you made on stops to the real parts (1-port faceplate 5, keystone jack white 39,
      blank faceplate), replace them on their stops, then **Remove from catalog**.
- [ ] Truck → Vendors: rename **DLV** to **Discount Low Voltage**.
- [ ] Truck → Parts → **Below cost**: review the 6 parts with management.

## 6. Data for HubSpot and QuickBooks
- [ ] Places → **HubSpot company IDs (CSV)**: download, fill in `hubspot_company_id`, **Check file**, then **Import**.
- [ ] Truck → Import / export: import the QuickBooks "Products and services" export. **Check file** first.

## 7. Test on the phone
- [ ] Home → **Go to a place** → Apple Maps or Google Maps. When you arrive, the stop is filled in.
      **Tell Claude** if it was not.
- [ ] Stop sheet:
  - [ ] It folds once the stop is named (✎ Edit opens it).
  - [ ] Type a departure time, then try **Follow GPS again**.
  - [ ] Part rows: − / + and **Bill**.
  - [ ] Quick phrases, and **Start from last visit** at a repeat customer.
  - [ ] Follow-up **owner dropdown**.
  - [ ] **HubSpot ticket**: paste a ticket link (it keeps the ID), or leave it blank and pick **This visit is: Continuing /
        New job / Not sure**.
  - [ ] **QuickBooks estimate #** on a quoted job.
  - [ ] **Finish stop**, then the **Next** button.
- [ ] Reports page:
  - [ ] Download PDF → **View here**: all pages, and Close works.
  - [ ] **Copy link for Safari**: paste it in Safari and the PDF opens.
  - [ ] **Email report** with JSON + PDF. Then send the same day again: the subject should say "(update 2)".
- [ ] A day with a fuel stop between two jobs: Total paid time includes both drives.

## 7b. New in this update (2026-11-23 evening)
- [ ] **Vendor stop → Bought here**: at a vendor (or an old vendor stop), open the stop. The Parts box is now **Bought
      here**: pick a part, quantity (tick "sticks of 6" for raceway), $ paid, Add. Truck → the part: stock went UP.
- [ ] An old vendor stop with parts on it shows **Move to Bought here**: tap it for each line (fixes the stock).
- [ ] Vendor receipt photo (AI reader on): "Items on the receipt" → **+ Add as bought**.
- [ ] **Delete permanently**: Truck → Parts → **Show removed** → open an old test part → **Delete permanently**. (Only
      shows for parts that are on no stop.)
- [ ] **Running late**: Settings → Alerts → Running late alert **on**, late by **10**. Settings → Email → **Send late
      notices to** = your agent's address (blank = your work email). Pushover must be set up, and Settings → Pushover →
      **App address for notification links** (e.g. http://192.168.128.245:8000) so the alert opens JobHub.
- [ ] On a day with an on-site appointment: when you are running late, Home shows a red box. Try **Tell the customer**
      once with a note and check the email your agent gets. **Tell Claude** if the alert came too early, too late, or never.

## 7c. Send for invoice
- [ ] Finish a **Time & Material** job, then in the sheet: **Invoice → Send for invoice…**. Check the preview (labor
      time, parts and prices), then **Send**. The stop then says "✓ Sent for invoice".
- [ ] Give your agent `invoice-request-sample.json` / `.pdf` / `invoice-request-body.txt` and the README section
      "Invoice requests" first, and have it describe the invoice it would create before it creates real ones.

## 7d. Message format 2 (built 2026-11-24)
- [ ] Places → Edit place for your regular customers: check **Street / Suite / City / State / ZIP** (filled in from the
      old address line; add the suite), and the optional **QuickBooks customer ID** and **Billing email**.
- [ ] A job's Finish step now has **Waiting for parts** and **Waiting for customer** (old "Waiting" answers became
      Waiting for parts: change any that were really waiting on the customer).
- [ ] Send for invoice: the preview has **Customer billing email** (optional, remembered for that place).
- [ ] Give your agent `docs/sample-report/v2/` and the README section "Message format 2".
- [ ] Reports → Email report with **Send as format 2 (test)** ticked for one real day; the agent checks it.
- [ ] When the agent is happy: Settings → Email → **Message format: 2**. After a week with no format 1 emails, tell
      Claude to remove format 1.
- [ ] Office: put the HubSpot ticket **link** on the calendar appointment's `Ticket:` line (short numbers like 1043
      can't be looked up in HubSpot).

## 8. Work AI agent
- [ ] Give it `docs/sample-report/` (the .eml, the JSON and README.md). Ask it to describe what it would write to HubSpot,
      without writing anything.
- [x] Then email it one real day and have it do the same dry run before it writes anything to HubSpot.
- [ ] Tell it to accept reports only from your JobHub Gmail address.
- [ ] Optional: Settings → Email → **Report signing key**; give the agent the same phrase and have it check the `Signature:` line.
- [ ] Give it the late notice samples too (`late-notice-sample.json`, `late-notice-body.txt`, README "Late notices") and
      ask how it would contact the customer.

## 9. Later
- [ ] **https** for JobHub (so Share works in the Home Screen app). You use a VPN for now; the long-term choice is open.
- [ ] Optional: Pushover alerts, the LanguageTool container.
- [ ] When you are ready: estimates (sample estimate email, quoted vs actual) and the agent's "Invoiced #…" reply on the stop.
