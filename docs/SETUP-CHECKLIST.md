# JobHub setup and test checklist (updated 2026-11-25)

Work down the list. Items marked **Tell Claude** are results to report back. Ticked items are ones you reported done.

## 1. Update the server
- [x] `git pull`, then `docker compose build` and `docker compose up -d` (repeat for every update).
- [x] On the iPhone, close the Home Screen app fully and open it again (loads the new pages).

## 1b. Check the update worked
- [ ] Settings → Log and backups → **View recent log** shows "JobHub started: …".
- [ ] Reports → Download PDF → **View here** shows every page.

## 2. Settings → Me and Pay rules
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
- [ ] JobHub Settings → My calendar: paste the link → **Test calendar**.
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
- [ ] **Running late**: Settings → My alerts → Running late alert **on**, late by **10**. Settings → Work AI agent → **Send late
      notices to** = your agent's address (blank = your work email). Pushover must be set up, and Settings → Address and access →
      **App address** (now your https address) so the alert opens JobHub.
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
- [ ] Give your agent `docs/sample-report/v2/` and the README section "Message format 2" (updated after its review:
      on-site invoice, revision 2 with removed items, calendar link, real SKU example).
- [ ] Reports → Email report with **Send as format 2 (test)** ticked for one real day; the agent checks it.
- [ ] When the agent is happy: Settings → Work AI agent → **Message format: 2**. After a week with no format 1 emails, tell
      Claude to remove format 1.
- [ ] Office: put the HubSpot ticket **link** on the calendar appointment's `Ticket:` line (short numbers like 1043
      can't be looked up in HubSpot).

## 8. Work AI agent
- [ ] Give it `docs/sample-report/` (the .eml, the JSON and README.md). Ask it to describe what it would write to HubSpot,
      without writing anything.
- [x] Then email it one real day and have it do the same dry run before it writes anything to HubSpot.
- [ ] Tell it to accept reports only from your JobHub Gmail address.
- [ ] Optional: Settings → Work AI agent → **Report signing key**; give the agent the same phrase and have it check the `Signature:` line.
- [ ] Give it the late notice samples too (`late-notice-sample.json`, `late-notice-body.txt`, README "Late notices") and
      ask how it would contact the customer.

## 9. Later
- [ ] Optional: Pushover alerts, the LanguageTool container.
- [ ] When you are ready: estimates (sample estimate email, quoted vs actual) and the agent's "Invoiced #…" reply on the stop.

## 10. Public access: login, then the Cloudflare tunnel (built 2026-11-25)
Do these **in order**, and the first part on your home network.
- [ ] Update the server (section 1). On your home network open JobHub → Settings → **Account and password** →
      create your username and password (12+ characters; a few words together is easiest) and your email for resets.
      `APP_PASSWORD` stops working from then on (you can remove it from `.env`).
- [ ] Sign out, sign in again on the PC and in the iPhone Home Screen app (both stay signed in for 30 days).
- [ ] Settings → Email must be set up for **Forgot password?** to work. Try it once: the link arrives, works once.
- [ ] Note the console reset, in case: `docker exec -it JobHub python -m app.auth reset <username>`.
- [ ] Cloudflare Zero Trust → Networks → Tunnels: create a tunnel, run `cloudflared` as a container on Unraid, and add a
      public hostname (e.g. `jobhub.<your domain>`) → service `http://192.168.128.245:8000`. Leave the Host header as is.
- [ ] Zero Trust → Access → Applications: a self-hosted app for that hostname, policy **Allow** = your email only.
      Session duration: 1 month (the Home Screen app then rarely asks). Do **not** add a Bypass rule for JobHub paths
      (not even `/health`): JobHub uses `/health` to notice an expired Cloudflare sign-in and reload the page.
- [ ] On the JobHub public hostname → Additional application settings → Access: turn on **Protect with Access**, so the
      tunnel refuses requests even if the Access policy is ever removed or set wrong. Cloudflare → SSL/TLS → Edge
      Certificates: **Always Use HTTPS** on. Only these two hostnames through the tunnel (no wildcard).
- [ ] JobHub Settings → Address and access → **App address**: `https://jobhub.<your domain>` (notification links and reset
      emails use it, and it is accepted as JobHub's own site for changes).
- [ ] **Home network (split DNS + NPM)**: your local DNS sends `jobhub.<your domain>` to NPM. NPM proxy host → forward
      `http` `192.168.128.245:8000`, a Let's Encrypt certificate (DNS challenge), Force SSL on, **Cache Assets off** (it
      keeps old page scripts after an update), no access list. At home you then get JobHub's login with no Cloudflare
      screen; if Cloudflare appears, the device is getting public DNS (IPv6 or HTTPS-type records, iCloud Private Relay,
      a private DNS profile). No router port forward may reach NPM: Cloudflare Access would not be in that path.
- [ ] Optional, NPM → JobHub host → Advanced: `access_log off;` (no record of visits in NPM; JobHub keeps its own log).
      Old NPM log files (`/data/logs/proxy-host-*_access.log`) from before this update (built 2026-11-26) can hold coordinates and search
      text: delete them once.
- [ ] On home Wi-Fi, delete the old Home Screen icon and add it again from `https://jobhub.<your domain>` (Share → Add to
      Home Screen). Use this one address at home and away.
- [ ] Test from the phone on cellular (Wi-Fi off): Cloudflare sign-in (type the emailed code into the app; a link in the
      email opens Safari instead), then JobHub sign-in; open a day, add a part, add a photo, Email report. Switch to Mail
      while the code screen is up and check it is still there when you return (if it is not, use Google as the Cloudflare
      sign-in method). Close the app fully and reopen: no new sign-in. Zero Trust → revoke your session, then tap
      something: the Cloudflare sign-in comes back and returns you to JobHub. **Tell Claude** if anything says "came from
      another website" or the sign-in loops.
- [ ] In the new Home Screen app: Reports → Download PDF → **View here** (every page, with **Share / Save** at the top)
      and **Share / Save to Files** (Save to Files, Mail, AirDrop). Also Truck → shopping list → Share.
- [ ] OwnTracks: its own hostname (e.g. `owntracks.<your domain>`) through the tunnel to NPM, with NPM's login (basic
      auth) on everything. In Cloudflare Access that hostname needs a **Bypass** policy (the OwnTracks app can't do the
      Cloudflare sign-in), so NPM's login is its only lock: use a long password. In OwnTracks: Mode HTTP, URL
      `https://owntracks.<your domain>/pub`, user and password. JobHub keeps reading the Recorder on the home network
      (Settings → OwnTracks Recorder stays as it is).
- [ ] On the OwnTracks public hostname set **Path** to `^/pub$` (catch-all rule: 404), so the internet reaches only the
      upload address, never the Recorder's pages and location history. From cellular: uploads still arrive and
      `https://owntracks.<your domain>/` does not load. If NPM forces SSL on that host, point the tunnel at
      `https://<NPM>:443` with Origin Server Name = the hostname (port 80 would loop and uploads would fail).
- [ ] Two-factor sign-in on Cloudflare and Gmail. NPM admin login changed from the default, port 81 home only. Guest and
      IoT networks can't reach the Unraid server. Backups and `appdata/jobhub` not on an open share (they hold the Gmail
      app password and API keys). Remove `APP_PASSWORD` from the container.
- [ ] Lost phone: JobHub → Account → **Sign out everywhere else**, remove its notification device, revoke its Cloudflare
      session, change the OwnTracks password.
- [ ] Keep the VPN as a way in if Cloudflare is ever down.

## 11. iPhone notifications instead of Pushover (built 2026-11-25)
Needs section 10 done (the https address) and iOS 16.4 or later.
- [ ] Update the server (section 1; this update adds a package, so the rebuild takes a little longer).
- [ ] Open JobHub from the Home Screen icon you added from the https address → Settings → **Account and password** →
      Notifications → **Turn on notifications on this device** → Allow.
- [ ] **Send test notification**: it should show on the lock screen within seconds; tapping it opens JobHub.
      **Tell Claude** if it doesn't (and what the device list says under "problem").
- [ ] Optional: do the same in Firefox on the PC (desktop notifications).
- [ ] Settings → My alerts → **Send alerts to** = both (the default) for a week; compare the two apps. Then set it to
      **iphone**, and Pushover can be cancelled.
- [ ] If notifications stop: iPhone Settings → Notifications → JobHub must be allowed; deleting the Home Screen icon
      turns them off (turn them on again after re-adding it).

## 12. Dashboard and uptime monitor (built 2026-11-28)
- [ ] On the server: `openssl rand -hex 32`, then add the result as the variable `STATUS_TOKEN` on the JobHub container
      (Unraid: edit the container → Add another Path, Port, Variable; compose: in `.env`). Update the server (section 1).
- [ ] `curl -i http://192.168.128.245:8000/healthz` answers 200 `{"status":"ok"}`; `/api/status` without the token 401;
      with `-H "Authorization: Bearer <token>"` the status JSON.
- [ ] Homepage widget and Uptime Kuma monitors: README → Status API. Put the token in Homepage's
      `HOMEPAGE_VAR_JOBHUB_STATUS_TOKEN`, not in `services.yaml`.

## 13. Security review update (built 2026-11-28)
- [ ] Update the server (section 1). The rebuild installs newer packages. Reload the browser and reopen the phone app.
- [ ] Day page: the map and numbered pins show; Find business → pick one → its name shows on the map.
- [ ] Settings: the groups open from links, and a Test button (calendar, email) answers.
- [ ] Home Screen app: Reports → a download → Other / Copy link for Safari → it opens in Safari.
- [ ] Settings → Account: saving the reset email now asks for the current password. **App address** is set (without it,
      “Forgot password?” sends a link only from a home network address).
- [ ] Cloudflare: leave **Rocket Loader** off (JobHub's pages only run their own scripts). **Tell Claude** if a page
      stops reacting to buttons.
