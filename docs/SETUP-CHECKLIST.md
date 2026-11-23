# JobHub setup and test checklist (2026-11-23)

Work down the list. Items marked **Tell Claude** are results to report back.

## 1. Update the server
- [ ] `git pull`, then `docker compose build` and `docker compose up -d` (a rebuild is needed: a new package was added).
- [ ] On the iPhone, close the Home Screen app fully and open it again (loads the new pages).

## 1b. Check the update worked
- [ ] Settings → General → Log → **View recent log** shows "JobHub started: …".
- [ ] Reports → Download PDF → **View here** shows every page.

## 2. Settings → General
- [ ] **Technician**: your name and work email.
- [ ] **Follow-up owners**: `Me` plus one line per person, e.g. `Office = office@yourco.com`.
- [ ] **Pay rules → Lunch is paid**: leave on unless the company says lunch is unpaid.

## 3. Gmail (reports and calendar invites)
- [ ] Create a separate Gmail for JobHub (e.g. `yourname.jobhub@gmail.com`).
- [ ] Turn on 2-Step Verification, then create an **App password** named JobHub.
- [ ] Settings → Email: Send reports to (your work email), the Gmail address, the App password. Save.
- [ ] **Send test email**. In Outlook, add the Gmail address to Safe senders and check junk.
- [ ] **Send test calendar invite**. **Tell Claude:** did it appear on its own, or did Outlook ask you to accept?
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
- [ ] **Or Claude**: get an Anthropic API key (console.anthropic.com) and set a monthly spend limit there.
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

## 8. Work AI agent
- [ ] Give it `docs/sample-report/` (the .eml, the JSON and README.md). Ask it to describe what it would write to HubSpot,
      without writing anything.
- [ ] Then email it one real day and have it do the same dry run before it writes anything to HubSpot.
- [ ] Tell it to accept reports only from your JobHub Gmail address.
- [ ] Optional: Settings → Email → **Report signing key**; give the agent the same phrase and have it check the `Signature:` line.

## 9. Later
- [ ] **https** for JobHub (so Share works in the Home Screen app). **Tell Claude** how you reach JobHub: home Wi-Fi only,
      or Tailscale / VPN.
- [ ] Optional: Pushover alerts, the LanguageTool container.
- [ ] Next build after testing: the sample estimate email for your agent (estimates, quoted vs actual).
