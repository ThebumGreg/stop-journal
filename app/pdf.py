"""Render a report (app/report.py) as a text-based PDF: real selectable text, plain tables,
fixed field labels, one timezone statement. No images, so a reader can extract everything."""
import io
import os
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
_registered = False


def _fonts():
    global _registered
    if not _registered:  # DejaVu covers far more characters than the built-in Helvetica
        pdfmetrics.registerFont(TTFont("JH", os.path.join(FONT_DIR, "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("JH-Bold", os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFontFamily("JH", normal="JH", bold="JH-Bold", italic="JH", boldItalic="JH-Bold")
        _registered = True


INSTRUCTIONS = [
    "This report lists the jobs worked on for the period above. Use it to update HubSpot.",
    "Top section: the report date or period and the day's totals. All times are 24-hour local time ({tz}). Totals cover "
    "only the confirmed stops listed below. On-site time is what is billable to customers. Lunch is paid to the "
    "technician but is not billable. Total paid time is on-site time plus lunch plus drive time. End Day is when the "
    "work day ended. If it ended at home, the drive home counts as work time; otherwise work time stopped there and the "
    "drive home is not work time (its miles are still in the mileage).",
    "Stop blocks: one per confirmed stop, in order. Work performed is the full text the technician wrote. Parts used "
    "lists parts consumed at the stop. Non-billable materials (supplies such as tape or solvent) are listed separately "
    "and are not for billing. Expenses (parking, tolls, purchases) are listed with their cost; Billable says whether "
    "they are charged to the customer. A blank HubSpot company ID means it is not known yet: match the company by "
    "business name and address.",
    "Category is one of seven: Time & Material (billed hourly), Quoted project and White-label migration (hours "
    "tracked against the project), and Internal operations, Administrative, Customer outreach and Training "
    "(non-billable). HubSpot ticket # is the service ticket number; QuickBooks estimate # is the estimate the job was "
    "quoted on (quoted projects and migrations). Visit result says whether the job is complete, needs a return visit, or is waiting on parts or the "
    "customer. Hours by category adds up on-site time only. A dash means nothing was entered.",
    "Follow-ups: what is still open, who owns it and when it is due. Create a task for each one.",
    "Drive from previous stop: the mileage leg that ends at this stop, from Home, Client, Office or Vendor (the kind of "
    "place the previous stop was; the first stop starts from Home). Minutes are "
    "shown only when that drive counted as work time; \"estimated: no location data\" means the phone sent no location points for part of the trip, so the minutes are a distance-based estimate. If the user stopped for lunch between two jobs, the drives before "
    "and after lunch are added together as one drive, and the miles are the direct distance between the two jobs. "
    "If the user left a site for lunch or a personal break and came back to it, that is shown as one stop: Time on "
    "site leaves out the lunch or break (listed as Lunch (non-billable) or Personal break (unpaid)), and the trip adds "
    "no miles or drive time.",
    "Records to update: one row per confirmed stop. This table is what to act on.",
    "Receipts and photos: pictures the technician attached, each labelled with its stop number and business (or "
    "fuel stop) and the file name it has in the zip export. The text in this report is the record; the pictures back it up.",
    "Fuel: fill-ups at gas stations (the truck's fuel, not billed to a customer). The time at the pump counts as "
    "drive time. Odometer is the reading the technician entered.",
    "Mileage: always measured Home to Home through the work stops in order, so it is what a customer may be billed. "
    "Personal stops and lunch are left out, so a detour to one is replaced by the direct road distance. Miles are "
    "approximate (road distance), not odometer readings. The total is the last row.",
    "Day recap (timesheet): the day in time order: on-site visits, drives that counted as work, traffic (a slow-down, "
    "counted as drive time), lunch (paid) and personal breaks (unpaid, in brackets: the technician clocked out). Times "
    "are hours and minutes. Home before and after the day is not listed. Entries marked Not confirmed were left out on "
    "purpose; do not create records for them. This table is for time keeping, not for HubSpot records.",
]


def _end_day_rows(e):
    if not e:
        return []
    where = {"home": "at home", "personal": "at a Personal stop", "job": "at " + (e["place"] or "the last stop")}[e["kind"]]
    row = [("End Day", f"{e['time']}, {where}")]
    if e["kind"] != "home":
        row.append(("Drive home", "not counted as work time (miles are in the mileage)"))
    return row


def _fuel_table(day, st, multi):
    """Fill-ups (Gas stops): station, gallons, total, price per gallon, odometer."""
    fl = day.get("fuel")
    if not fl:
        return []
    B, Rt = st["small"], ParagraphStyle("rs", parent=st["small"], alignment=2)
    num = lambda v, f: _p("" if v is None else format(v, f), Rt)
    rows = [[_p(f["arrival"], B), _p(f["station"] or "Fuel stop", B), num(f["gallons"], ".3f"), num(f["total"], ".2f"),
             num(f["price_per_gallon"], ".3f"), num(f["odometer"], "d")] for f in fl]
    title = "Fuel" + (f": {day['date']}" if multi else "")
    return [Paragraph(title, st["h1"]),
            _grid(["Time", "Station", "Gallons", "Total", "Price / gal", "Odometer"], rows,
                  [0.7 * inch, 2.6 * inch, 0.8 * inch, 0.9 * inch, 0.9 * inch, 1.1 * inch], st)]


def _mileage_table(day, st):
    """Mileage as a table (#, From, To, Miles) with the total in the last row."""
    m = day.get("mileage")
    if not m:
        return []
    rows = [[_p(str(i), st["base"]), _p(lg["from"], st["base"]), _p(lg["to"], st["base"]),
             _p(f"{lg['miles']:.1f}", st["right"])] for i, lg in enumerate(m["legs"], 1)]
    rows.append([_p("", st["base"]), Paragraph("Total mileage", st["label"]), _p("", st["base"]),
                 Paragraph(f"{m['total_miles']:.1f}", st["right_bold"])])
    t = _grid(["#", "From", "To", "Miles"], rows, [0.4 * inch, 2.9 * inch, 2.9 * inch, 0.8 * inch], st)
    t.setStyle(TableStyle([("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),
                           ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f6f6f6"))]))
    return [Paragraph("Mileage (Home to Home, miles)", st["h1"]), t]


def _dur(minutes):
    """Hour/minute format: '3 h 48 min', '41 min'."""
    return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min"


def _recap(day, st, multi):
    """Day recap laid out like a timesheet: in, out, activity, time. Lunch is paid; a personal break is unpaid
    (in brackets). Home before and after the day is not listed. Totals and the mileage close it."""
    sheet = day.get("timesheet") or []
    stops = day["stops"]
    start = day.get("day_start") or (stops[0]["arrival"] if stops else "")
    end = day.get("day_end") or (stops[-1]["departure"] if stops else "")
    B = st["base"]
    rows = []
    for r in sheet:
        t = _dur(r["minutes"]) if r["paid"] else f"({_dur(r['minutes'])})"
        rows.append([_p(r["in"], B), _p(r["out"], B), _p(r["description"], B), _p(t, st["right"])])
    if not rows:
        rows.append([_p("", B), _p("", B), _p("No paid time recorded.", B), _p("", st["right"])])
    on_site = sum(r["minutes"] for r in sheet if r["type"] == "on_site")
    drive = sum(r["minutes"] for r in sheet if r["type"] == "drive")
    lunch = sum(r["minutes"] for r in sheet if r["type"] == "lunch")
    unpaid = sum(r["minutes"] for r in sheet if r["type"] == "break")
    totals = [("On site", _dur(on_site))]
    if lunch:
        totals.append(("Lunch", _dur(lunch)))
    totals += [("Drive", _dur(drive)), ("Total paid time", _dur(on_site + drive + lunch))]
    if unpaid:
        totals.append(("Personal breaks (unpaid, not included)", f"({_dur(unpaid)})"))
    m = day.get("mileage")
    if m:
        totals.append(("Mileage, Home to Home", f"{m['total_miles']:.1f} mi"))
    first_tot = len(rows)
    for label, val in totals:
        bold = label in ("Total paid time",)
        rows.append([_p("", B), _p("", B), Paragraph(label, st["label"]),
                     Paragraph(val, st["right_bold"] if bold else st["right"])])
    t = _grid(["In", "Out", "Activity", "Time"], rows, [0.7 * inch, 0.7 * inch, 4.2 * inch, 1.4 * inch], st)
    t.setStyle(TableStyle([("LINEABOVE", (0, first_tot + 1), (-1, first_tot + 1), 1, colors.black),
                           ("BACKGROUND", (0, first_tot + 1), (-1, -1), colors.HexColor("#f6f6f6"))]))
    title = "Day recap (timesheet)" + (f": {day['date']}" if multi else "")
    head = _p(f"Clock in {start or '—'}  ·  Clock out {end or '—'}  (day framed Home to Home)", B)
    return [KeepTogether([Paragraph(title, st["h1"]), head, Spacer(1, 3), t])]


def _styles():
    base = ParagraphStyle("base", fontName="JH", fontSize=9.5, leading=12.5)
    return {
        "base": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=10),
        "right": ParagraphStyle("right", parent=base, alignment=2),
        "right_bold": ParagraphStyle("right_bold", parent=base, fontName="JH-Bold", alignment=2),
        "title": ParagraphStyle("title", parent=base, fontName="JH-Bold", fontSize=17, leading=21),
        "h1": ParagraphStyle("h1", parent=base, fontName="JH-Bold", fontSize=13, leading=16, spaceBefore=12,
                             spaceAfter=4, keepWithNext=True),
        "h2": ParagraphStyle("h2", parent=base, fontName="JH-Bold", fontSize=11, leading=14, spaceBefore=8,
                             spaceAfter=3, keepWithNext=True),
        "label": ParagraphStyle("label", parent=base, fontName="JH-Bold"),
        "th": ParagraphStyle("th", parent=base, fontName="JH-Bold", fontSize=8, leading=10),
    }


def _p(text, style):
    """Plain user text -> Paragraph (escaped, newlines kept, empty shown as a dash)."""
    return Paragraph(_html(text), style)


def _html(text):
    t = escape(str(text)) if text not in (None, "") else "&mdash;"
    return t.replace("\r\n", "\n").replace("\n", "<br/>")


def _fit(text, style, width, max_h):
    """Split text into pieces that each *measure* no taller than max_h at this column width
    (a table row cannot span pages). Halves by lines, then words, then characters."""
    text = (text or "").replace("\r\n", "\n")
    para = Paragraph(_html(text), style)
    para.wrap(width, 100000)
    if para.height <= max_h or len(text) < 2:
        return [text]
    for sep in ("\n", " "):
        parts = text.split(sep)
        if len(parts) > 1:
            mid = len(parts) // 2
            return (_fit(sep.join(parts[:mid]), style, width, max_h) +
                    _fit(sep.join(parts[mid:]), style, width, max_h))
    mid = len(text) // 2
    return _fit(text[:mid], style, width, max_h) + _fit(text[mid:], style, width, max_h)


MAX_ROW_H = 560  # points; a page frame is ~672 tall


def _hm(minutes):
    return f"{minutes} min ({minutes // 60} h {minutes % 60:02d} min)" if minutes >= 60 else f"{minutes} min"


def _kv(rows, st, label_w=1.75 * inch, width=7 * inch):
    t = Table([[Paragraph(escape(k), st["label"]), v if not isinstance(v, str) else _p(v, st["base"])]
               for k, v in rows], colWidths=[label_w, width - label_w])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                           ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    return t


def _grid(header, rows, widths, st):
    data = [[Paragraph(escape(h), st["th"]) for h in header]] + rows
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                           ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    return t


def _parts_table(parts, st):
    if not parts:
        return _p("None", st["base"])
    costs = all("unit_cost" in p for p in parts)     # left out when the export omits costs
    rows = []
    for p in parts:
        row = [_p(p["name"], st["small"]), _p(p["part_number"], st["small"]),
               _p(f"{p['quantity']:g}", st["small"]), _p(p["unit"], st["small"])]
        if costs:
            row.append(_p("" if p["unit_cost"] is None else f"{p['unit_cost']:.2f}", st["small"]))
        rows.append(row + [_p("yes" if p["billable"] else "no", st["small"])])
    head = ["Part", "Part #", "Qty", "Unit"] + (["Unit cost"] if costs else []) + ["Billable"]
    widths = [1.65, 0.9, 0.45, 0.5] + ([0.8] if costs else []) + [0.75]
    if not costs:
        widths[0] += 0.8
    return _grid(head, rows, [w * inch for w in widths], st)


_BILLING = {"hourly": "billed hourly", "project": "tracked against the project", "non_billable": "non-billable"}


def _money(v):
    return "" if v is None else f"{v:.2f}"


def _expenses_table(items, st):
    rows = [[_p(e["name"], st["small"]), _p(f"{e['quantity']:g}", st["small"]), _p(_money(e["unit_cost"]), st["small"]),
             _p(_money(e["line_cost"]), st["small"]), _p("yes" if e["billable"] else "no", st["small"])] for e in items]
    total = sum(e["line_cost"] or 0 for e in items)
    rows.append([Paragraph("Total", st["th"]), _p("", st["small"]), _p("", st["small"]),
                 Paragraph(f"{total:.2f}", st["th"]), _p("", st["small"])])
    return _grid(["Expense", "Qty", "Unit cost", "Cost", "Billable"], rows,
                 [1.85 * inch, 0.45 * inch, 0.8 * inch, 0.8 * inch, 0.75 * inch], st)


def _followups_table(items, st, extra=None, total=7.0):
    head = ["Follow-up", "Owner", "Due"]
    widths = [3.5, 1.0, 0.9]
    if extra:
        head.insert(0, "Business")
        widths.insert(0, 1.4)
    scale = total / sum(widths)
    widths = [w * scale * inch for w in widths]
    rows = []
    for f in items:
        row = [_p(f["what"], st["small"]), _p(f["owner"], st["small"]), _p(f["due"], st["small"])]
        if extra:
            row.insert(0, _p(f["business_name"], st["small"]))
        rows.append(row)
    return _grid(head, rows, widths, st)


def _category_table(day, st):
    rows = day["summary"].get("hours_by_category") or []
    if not rows:
        return []
    bill = {"hourly": "Billed hourly", "project": "Tracked against the project", "non_billable": "Non-billable", "": "—"}
    body = [[_p(r["category"], st["base"]), _p(bill.get(r["billing"], "—"), st["base"]),
             _p(_hm(r["on_site_minutes"]), st["right"])] for r in rows]
    return [Spacer(1, 6), Paragraph("Hours by category (on-site time)", st["h2"]),
            _grid(["Category", "Billing", "On-site time"], body, [2.4 * inch, 2.6 * inch, 2.0 * inch], st)]


LABEL_W = 2.2 * inch


def _stop_block(s, st):
    drive = s["drive_from_previous_stop"]
    if not drive:
        drive_txt = "n/a"
    else:
        origin = drive.get("from_type") or drive.get("from")      # the kind of place, not its name
        drive_txt = f"{drive['miles_approx']} mi" + (f" from {origin}" if origin else "")
        if drive["minutes"] is not None:
            drive_txt += f", {drive['minutes']} min" + (" (estimated: no location data)" if drive.get("estimated") else "")
    if drive and drive["notes"]:
        drive_txt += f". Drive notes: {drive['notes']}"
    latlon = None if "latitude" not in s else (
        "(no GPS data)" if s["latitude"] is None else f"{s['latitude']:.6f}, {s['longitude']:.6f}")
    rows = [("Stop number", str(s["stop_number"])), ("Business name", s["business_name"]),
            ("Address", s["address"])]
    if s["place_type"]:
        rows.append(("Place type", s["place_type"]))
    lunch = s.get("lunch_non_billable") or []
    on_site = _hm(s["time_on_site_minutes"]) + (" (lunch and breaks not included)" if lunch or s.get("personal_breaks_unpaid") else "")
    if latlon is not None:                                   # left out when "GPS coordinates" is unticked
        rows.append(("Latitude/Longitude", latlon))
    rows += [("Arrival", s["arrival"]), ("Departure", s["departure"]), ("Time on site", on_site)]
    if lunch:
        rows.append(("Lunch (non-billable)", "; ".join(f"{x['from']} to {x['to']}, {x['minutes']} min" for x in lunch)))
    brk = s.get("personal_breaks_unpaid") or []
    if brk:
        rows.append(("Personal break (unpaid)", "; ".join(f"{x['from']} to {x['to']}, {x['minutes']} min" for x in brk)))
    rows += [("Drive from previous stop", drive_txt),
             ("HubSpot company ID", s["hubspot_company_id"]),
             ("Category", s["category"] + (f" ({_BILLING[s['billing']]})" if s["category"] and s["billing"] else "")),
             ("HubSpot ticket #", s.get("hubspot_ticket", s["job_reference"])),
             ("QuickBooks estimate #", s.get("qbo_estimate", "")), ("Visit result", s["visit_result"])]
    for i, piece in enumerate(_fit(s["work_performed"], st["base"], 7 * inch - LABEL_W - 12, MAX_ROW_H)):
        rows.append(("Work performed" if i == 0 else "Work performed (continued)", piece))
    rows.append(("Parts used", _parts_table(s["parts_used"], st)))
    if s.get("consumables_used"):
        rows.append(("Non-billable materials", _parts_table(s["consumables_used"], st)))
    if s.get("expenses"):
        rows.append(("Expenses", _expenses_table(s["expenses"], st)))
    if s.get("follow_ups"):
        rows.append(("Follow-ups", _followups_table(s["follow_ups"], st, total=4.7)))
    if s.get("photos"):
        n = len(s["photos"])
        rows.append(("Photos", f"{n} attached (see Receipts and photos)"))
    return [Paragraph(f"Stop {s['stop_number']}", st["h2"]), _kv(rows, st, label_w=LABEL_W)]


def _photos(report, st, load):
    """Each attached picture on the page with its label above it; at most about two per page."""
    items = [p for d in report["days"] for p in d.get("photos") or []]
    if not items or not load:
        return []
    out = [Paragraph("Receipts and photos", st["h1"])]
    for p in items:
        data = load(p["id"])
        if not data:
            continue
        img = Image(io.BytesIO(data))
        w, h = img.imageWidth, img.imageHeight
        k = min(7 * inch / w, 4.2 * inch / h, 1.0)
        img.drawWidth, img.drawHeight = w * k, h * k
        img.hAlign = "LEFT"
        out.append(KeepTogether([_p(p["label"], st["label"]), _p(f"File: {p['file']}", st["small"]),
                                 Spacer(1, 3), img, Spacer(1, 10)]))
    return out


def render(report, load_photo=None):
    _fonts()
    st = _styles()
    multi = report["date_from"] != report["date_to"]
    buf = io.BytesIO()

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("JH", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(0.75 * inch, 0.5 * inch, f"JobHub daily summary, schema version {report['schema_version']}")
        canvas.drawRightString(letter[0] - 0.75 * inch, 0.5 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.7 * inch, bottomMargin=0.8 * inch, title="JobHub daily summary",
                            author="JobHub")
    story = [Paragraph("JobHub Daily Summary", st["title"]), Spacer(1, 6)]
    period = (f"{report['date_from']} to {report['date_to']}" if multi else
              datetime.fromisoformat(report["date_from"]).strftime("%A, %Y-%m-%d"))
    gen = datetime.fromisoformat(report["generated_at"]).strftime("%Y-%m-%d %H:%M")
    head_rows = [("Period" if multi else "Date", period)]
    tech = report.get("technician") or {}
    if tech.get("name") or tech.get("email"):
        head_rows.append(("Technician", " ".join(x for x in (tech.get("name"), f"({tech['email']})" if tech.get("email") else "") if x)))

    def summary_rows(day):
        sm = day["summary"]
        rows = [("On-site time (billable)", _hm(sm["on_site_minutes"]))]
        if sm.get("lunch_minutes"):
            rows.append(("Lunch (paid, not billed)", _hm(sm["lunch_minutes"])))
        rows += [("Drive time", _hm(sm["drive_minutes"])),
                 ("Total paid time", _hm(sm.get("paid_minutes", sm["on_site_minutes"] + sm["drive_minutes"]))),
                 ("Approximate miles", f"{sm['miles_approx']} mi (approximate)"),
                 ("Stops confirmed", str(sm["stops_confirmed"]))]
        if sm.get("expenses_total"):
            rows.append(("Expenses", f"{sm['expenses_total']:.2f}"))
        if sm.get("fuel_total"):
            rows.append(("Fuel", f"{sm['fuel_total']:.2f}"))
        if day.get("day_start") or day.get("day_end"):
            rows.append(("Work day", f"{day.get('day_start') or '—'} to {day.get('day_end') or '—'}"))
        return rows + _end_day_rows(day.get("end_day"))

    if not multi:       # one day: the header and the day summary are a single section
        story.append(_kv(head_rows + summary_rows(report["days"][0]), st, label_w=2.2 * inch))
        story.extend(_category_table(report["days"][0], st))
    else:
        story.append(_kv(head_rows, st))

    for day in report["days"]:
        d = day["date"]
        if multi:
            story.append(Paragraph(f"Day summary: {datetime.fromisoformat(d).strftime('%A, %Y-%m-%d')}", st["h1"]))
            story.append(_kv(summary_rows(day), st, label_w=2.2 * inch))
            story.extend(_category_table(day, st))
        if not day["stops"]:
            story.append(Spacer(1, 4))
            story.append(_p("No confirmed stops.", st["base"]))
        for s in day["stops"]:
            blk = _stop_block(s, st)
            story.extend(blk)

    story.append(Paragraph("Records to update", st["h1"]))
    head = ["Business name", "Address", "Arrival", "Departure", "Category", "Ticket / estimate", "Work performed",
            "Parts used"]
    widths = [0.95, 0.95, 0.7, 0.95, 0.85, 0.7, 1.4, 1.2]
    if multi:
        head.insert(0, "Date")
        widths.insert(0, 0.8)
    scale = 7.0 / sum(widths)
    widths = [w * scale * inch for w in widths]
    work_w = widths[head.index("Work performed")] - 12
    rows = []
    for r in report["records_to_update"]:
        parts = "; ".join(r["parts_used"]) if r["parts_used"] else ""
        for i, piece in enumerate(_fit(r["work_performed"], st["small"], work_w, MAX_ROW_H)):
            if i == 0:
                row = [_p(r["business_name"], st["small"]), _p(r["address"], st["small"]),
                       _p(r["arrival"], st["small"]), _p(r["departure"], st["small"]),
                       _p(r["category"], st["small"]),
                       _p(" / ".join(x for x in (r.get("hubspot_ticket", r["job_reference"]), r.get("qbo_estimate", "")) if x),
                          st["small"]),
                       _p(piece, st["small"]), _p(parts, st["small"])]
            else:  # long text continues on following rows (a table row cannot span pages)
                blank = Paragraph("", st["small"])
                row = [_p(f"(continued) {r['business_name']}", st["small"]), blank, blank, blank, blank, blank,
                       _p(piece, st["small"]), blank]
            if multi:
                row.insert(0, _p(r["date"], st["small"]))
            rows.append(row)
    if rows:
        story.append(_grid(head, rows, widths, st))
    else:
        story.append(_p("No confirmed stops in this period.", st["base"]))

    if "follow_ups" not in report:
        pass                                  # left out of this export
    elif report["follow_ups"]:
        story.append(Paragraph("Follow-ups", st["h1"]))
        fl = [dict(f, what=f["what"], business_name=(f["date"] + " " if multi else "") + f["business_name"])
              for f in report["follow_ups"]]
        story.append(_followups_table(fl, st, extra=True))
    else:
        story.append(Paragraph("Follow-ups", st["h1"]))
        story.append(_p("None.", st["base"]))

    for day in report["days"]:
        story.extend(_fuel_table(day, st, multi))
    for day in report["days"]:
        story.extend(_mileage_table(day, st))
        if "timesheet" in day:
            story.extend(_recap(day, st, multi))

    story.extend(_photos(report, st, load_photo))
    gone = {o["key"] for o in report.get("omitted") or []}
    if not (load_photo and any(d.get("photos") for d in report["days"])):
        gone.add("photos")
    if not any(d.get("fuel") for d in report["days"]):
        gone.add("fuel")
    skip = tuple(h for k, h in (("follow_ups", "Follow-ups:"), ("mileage", "Mileage:"), ("recap", "Day recap"),
                                ("fuel", "Fuel:"), ("photos", "Receipts and photos:")) if k in gone)   # no reading notes for a section that is not there
    notes = [t.replace("{tz}", report["timezone"]) for t in INSTRUCTIONS if not (skip and t.startswith(skip))]
    if report.get("omitted"):
        notes.append("Left out of this export on purpose (not missing data): " +
                     ", ".join(o["label"] for o in report["omitted"]) + ".")

    story.append(KeepTogether([Paragraph("How to read this report", st["h1"])] +
                              [_p(f"{i}. {t}", st["small"]) for i, t in enumerate(notes, 1)]))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
