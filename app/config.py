import os
from dataclasses import dataclass


def _int(name, default):
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    recorder_url: str
    recorder_user: str
    recorder_device: str
    recorder_auth: str
    app_tz: str
    places_provider: str
    google_places_key: str
    stop_min_minutes: int
    stop_radius_m: int
    stop_max_gap_min: int
    stop_merge_gap_min: int
    max_acc_m: int
    app_password: str
    data_dir: str
    places_radius_m: int = 150
    overpass_url: str = "https://overpass-api.de/api/interpreter, https://overpass.kumi.systems/api/interpreter"
    google_daily_cap: int = 100
    google_fallback_min: int = 3
    pushover_token: str = ""
    pushover_user: str = ""
    app_url: str = ""
    notify_channel: str = "both"   # both | iphone | pushover (each only when set up)
    alert_no_data: str = "on"
    alert_no_data_min: int = 60
    work_days: str = "Mon-Fri"
    work_start: str = "07:00"
    work_end: str = "17:00"
    alert_repeat_min: int = 0
    backup_keep: int = 7
    alert_drafts: str = "on"
    stop_short_min_s: int = 180
    alert_low_stock: str = "on"
    alert_left_stop: str = "on"
    alert_late: str = "on"         # Pushover when the ETA says you will be late for an on-site appointment
    alert_late_min: int = 10
    late_notice_to: str = ""       # where "Tell the customer" emails the late notice (blank = MAIL_TO)
    message_format: str = "1"      # JSON layout of the emails to the agent: "1" or "2" (docs/sample-report/README.md)
    calendar_url: str = ""       # published ICS link (secret); "" = off
    calendar_keyword: str = "onsite"
    receipt_reader: str = "off"       # off | google_vision (an AI reader can be added later)
    receipt_key: str = ""             # blank = use the Google Places key
    receipt_monthly_cap: int = 300
    anthropic_key: str = ""          # AI notes (Settings → AI); blank = off
    ai_notes: str = "on"
    ai_monthly_cap: int = 300
    note_phrases: str = ("Tested, all working.\nCustomer signed off.\nCleaned up the work area.\n"
                         "Explained the work to the customer.\nWill return with parts.")
    routing_url: str = ""    # "" = off; from_env defaults to the public OSRM server
    languagetool_url: str = ""   # "" = grammar check off
    auto_sync_min: int = 2       # day page for today syncs this often while open; 0 = off
    smtp_host: str = "smtp.gmail.com"   # Email report (Settings → Email)
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""      # secret (a Gmail App password)
    mail_from: str = ""          # blank = smtp_user
    mail_to: str = ""            # where reports go; blank = email off
    tech_name: str = ""          # who did the work (report `technician`)
    tech_email: str = ""
    qbo_service_with_sku: str = "on"   # QuickBooks import: Service items with a SKU are parts
    lunch_paid: str = "on"         # off: lunch is unpaid (left out of paid time)
    followup_owners: str = "Me"     # one per line: "Label = email" (Me = the technician email)
    log_level: str = "info"        # info | debug (Settings → General → Log)
    report_signing_key: str = ""   # secret: emailed reports carry an HMAC-SHA256 of the JSON file
    calendar_invites: str = "off"   # today's stops as invites to mail_to (Settings → Email)
    calendar_invite_min: int = 10

    @classmethod
    def from_env(cls):
        e = os.environ.get
        return cls(
            recorder_url=e("RECORDER_URL", "").rstrip("/"),
            recorder_user=e("RECORDER_USER", ""),
            recorder_device=e("RECORDER_DEVICE", ""),
            recorder_auth=e("RECORDER_AUTH", ""),
            app_tz=e("APP_TZ", "America/Los_Angeles"),
            places_provider=e("PLACES_PROVIDER", "osm"),
            google_places_key=e("GOOGLE_PLACES_KEY", ""),
            stop_min_minutes=_int("STOP_MIN_MINUTES", 10),
            stop_radius_m=_int("STOP_RADIUS_M", 100),
            stop_max_gap_min=_int("STOP_MAX_GAP_MIN", 15),
            stop_merge_gap_min=_int("STOP_MERGE_GAP_MIN", 60),
            max_acc_m=_int("MAX_ACC_M", 100),
            app_password=e("APP_PASSWORD", ""),
            data_dir=e("DATA_DIR", "/data"),
            places_radius_m=_int("PLACES_RADIUS_M", 150),
            overpass_url=e("OVERPASS_URL", "https://overpass-api.de/api/interpreter, https://overpass.kumi.systems/api/interpreter"),
            google_daily_cap=_int("GOOGLE_DAILY_CAP", 100),
            google_fallback_min=_int("GOOGLE_FALLBACK_MIN", 3),
            pushover_token=e("PUSHOVER_TOKEN", ""),
            pushover_user=e("PUSHOVER_USER", ""),
            app_url=e("APP_URL", "").rstrip("/"),
            notify_channel=e("NOTIFY_CHANNEL", "both"),
            alert_no_data=e("ALERT_NO_DATA", "on"),
            alert_no_data_min=_int("ALERT_NO_DATA_MIN", 60),
            work_days=e("WORK_DAYS", "Mon-Fri"),
            work_start=e("WORK_START", "07:00"),
            work_end=e("WORK_END", "17:00"),
            alert_repeat_min=_int("ALERT_REPEAT_MIN", 0),
            backup_keep=_int("BACKUP_KEEP", 7),
            alert_drafts=e("ALERT_DRAFTS", "on"),
            stop_short_min_s=_int("STOP_SHORT_MIN_S", 180),
            alert_low_stock=e("ALERT_LOW_STOCK", "on"),
            alert_left_stop=e("ALERT_LEFT_STOP", "on"),
            alert_late=e("ALERT_LATE", "on"),
            alert_late_min=_int("ALERT_LATE_MIN", 10),
            late_notice_to=e("LATE_NOTICE_TO", "").strip(),
            message_format=e("MESSAGE_FORMAT", "1").strip() if e("MESSAGE_FORMAT", "1").strip() in ("1", "2") else "1",
            calendar_url=e("CALENDAR_URL", "").strip(),
            calendar_keyword=e("CALENDAR_KEYWORD", "onsite").strip() or "onsite",
            receipt_reader=e("RECEIPT_READER", "off").strip() or "off",
            receipt_key=e("RECEIPT_KEY", "").strip(),
            receipt_monthly_cap=_int("RECEIPT_MONTHLY_CAP", 300),
            anthropic_key=e("ANTHROPIC_API_KEY", "").strip(),
            ai_notes=e("AI_NOTES", "on").strip() or "on",
            ai_monthly_cap=_int("AI_MONTHLY_CAP", 300),
            **({"note_phrases": e("NOTE_PHRASES", "").replace("|", "\n")} if e("NOTE_PHRASES", "") else {}),
            routing_url=e("ROUTING_URL", "https://router.project-osrm.org").strip().rstrip("/"),
            languagetool_url=e("LANGUAGETOOL_URL", "").strip().rstrip("/"),
            auto_sync_min=_int("AUTO_SYNC_MIN", 2),
            smtp_host=e("SMTP_HOST", "smtp.gmail.com").strip(),
            smtp_port=_int("SMTP_PORT", 587),
            smtp_user=e("SMTP_USER", "").strip(),
            smtp_password=e("SMTP_PASSWORD", "").strip(),
            mail_from=e("MAIL_FROM", "").strip(),
            mail_to=e("MAIL_TO", "").strip(),
            tech_name=e("TECH_NAME", "").strip(),
            qbo_service_with_sku=e("QBO_IMPORT_SERVICE_ITEMS_WITH_SKU", "on").strip() or "on",
            lunch_paid=e("LUNCH_PAID", "on").strip() or "on",
            tech_email=e("TECH_EMAIL", "").strip(),
            **({"followup_owners": e("FOLLOWUP_OWNERS", "").replace("|", "\n")} if e("FOLLOWUP_OWNERS", "") else {}),
            log_level=e("LOG_LEVEL", "info").strip().lower() or "info",
            report_signing_key=e("REPORT_SIGNING_KEY", "").strip(),
            calendar_invites=e("CALENDAR_INVITES", "off").strip() or "off",
            calendar_invite_min=_int("CALENDAR_INVITE_MIN", 10),
        )
