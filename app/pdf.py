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
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
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
    "Top section: report date or period, timezone (all times are 24-hour local time in that timezone), when it was "
    "generated, the report schema version, and the day's totals. Totals cover only the confirmed stops listed below. "
    "End Day is when the work day ended. If it ended at home, the drive home counts as work time; otherwise work time "
    "stopped there and the drive home is not work time (its miles are still in the mileage).",
    "Mileage: always measured Home to Home through the work stops in order, so it is what a customer may be billed. "
    "Personal stops and lunch are left out, so a detour to one is replaced by the direct road distance. Miles are "
    "approximate (road distance), not odometer readings. The total is the last row.",
    "Stop blocks: one per confirmed stop, in order. Work performed is the full text the technician wrote. Parts used "
    "lists parts consumed at the stop. Non-billable materials (supplies such as tape or solvent) are listed separately "
    "and are not for billing. A blank HubSpot company ID means it is not known yet: match the company by business name "
    "and address.",
    "Drive from previous stop: the mileage leg that ends at this stop (the first stop starts from Home). Minutes are "
    "shown only when that drive counted as work time. If the user stopped for lunch between two jobs, the drives before "
    "and after lunch are added together as one drive, and the miles are the direct distance between the two jobs. "
    "If the user left a site for lunch and came back to it, that is shown as one stop: Time on site leaves out the lunch "
    "(listed as Lunch (non-billable)), and the lunch trip adds no miles or drive time.",
    "Records to update: one row per confirmed stop. This table is what to act on.",
    "Day recap: the day framed Home to Home, with any lunch, traffic or unconfirmed entries by time only. They were left "
    "out on purpose; do not create records for them. A Traffic entry was a slow-down, not a visit: its minutes are "
    "already included in drive time.",
]


def _end_day_rows(e):
    if not e:
        return []
    where = {"home": "at home", "personal": "at a Personal stop", "job": "at " + (e["place"] or "the last stop")}[e["kind"]]
    row = [("End Day", f"{e['time']}, {where}")]
    if e["kind"] != "home":
        row.append(("Drive home", "not counted as work time (miles are in the mileage)"))
    return row


def _mileage_table(day, st):
    """Mileage as label/value rows like the rest of the report, the total last."""
    m = day.get("mileage")
    if not m:
        return []
    rows = [(f"{lg['from']} to {lg['to']}", f"{lg['miles']} mi") for lg in m["legs"]]
    rows.append(("Total mileage", f"{m['total_miles']} mi"))
    return [Paragraph("Mileage", st["h1"]), _kv(rows, st)]


def _recap(day, st, multi):
    """Day recap: the day framed Home to Home. Personal stops are not listed, whatever the real start or end was."""
    m = day.get("mileage")
    stops = day["stops"]
    start = day.get("day_start") or (stops[0]["arrival"] if stops else "")
    end = day.get("day_end") or (stops[-1]["departure"] if stops else "")
    rows = [("Start", f"Home, {start}" if start else "Home")]
    for e in sorted(day["excluded"], key=lambda x: x["arrival"]):
        if e["reason"] in ("Personal", "End Day"):
            continue
        rows.append((f"{e['arrival']} to {e['departure']}", e["reason"]))
    last = m["legs"][-1] if m else None
    end_txt = "Home" + (f", {end}" if end else "")
    if last:
        end_txt += f". {last['miles']} mi from {last['from']}"
    rows.append(("End", end_txt))
    if m:
        rows.append(("Total mileage", f"{m['total_miles']} mi"))
    title = "Day recap" + (f": {day['date']}" if multi else "")
    return [Paragraph(title, st["h1"]), _kv(rows, st)]


def _styles():
    base = ParagraphStyle("base", fontName="JH", fontSize=9.5, leading=12.5)
    return {
        "base": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=10),
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
    return f"{minutes} min ({minutes // 60} h {minutes % 60:02d} min)"


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
    rows = [[_p(p["name"], st["small"]), _p(p["part_number"], st["small"]),
             _p(f"{p['quantity']:g}", st["small"]), _p(p["unit"], st["small"]),
             _p("" if p["unit_cost"] is None else f"{p['unit_cost']:.2f}", st["small"]),
             _p("yes" if p["billable"] else "no", st["small"])] for p in parts]
    return _grid(["Part", "Part #", "Qty", "Unit", "Unit cost", "Billable"], rows,
                 [1.65 * inch, 0.9 * inch, 0.45 * inch, 0.5 * inch, 0.8 * inch, 0.75 * inch], st)


def _stop_block(s, st):
    drive = s["drive_from_previous_stop"]
    if not drive:
        drive_txt = "n/a"
    else:
        drive_txt = f"{drive['miles_approx']} mi" + (f" from {drive['from']}" if drive.get("from") else "")
        if drive["minutes"] is not None:
            drive_txt += f", {drive['minutes']} min"
    if drive and drive["notes"]:
        drive_txt += f". Drive notes: {drive['notes']}"
    latlon = "(no GPS data)" if s["latitude"] is None else f"{s['latitude']:.6f}, {s['longitude']:.6f}"
    rows = [("Stop number", str(s["stop_number"])), ("Business name", s["business_name"]),
            ("Address", s["address"])]
    if s["place_type"]:
        rows.append(("Place type", s["place_type"]))
    lunch = s.get("lunch_non_billable") or []
    on_site = _hm(s["time_on_site_minutes"]) + (" (lunch not included)" if lunch else "")
    rows += [("Latitude/Longitude", latlon), ("Arrival", s["arrival"]), ("Departure", s["departure"]),
             ("Time on site", on_site)]
    if lunch:
        rows.append(("Lunch (non-billable)", "; ".join(f"{x['from']} to {x['to']}, {x['minutes']} min" for x in lunch)))
    rows += [("Drive from previous stop", drive_txt),
             ("HubSpot company ID", s["hubspot_company_id"])]
    for i, piece in enumerate(_fit(s["work_performed"], st["base"], 7 * inch - 1.75 * inch - 12, MAX_ROW_H)):
        rows.append(("Work performed" if i == 0 else "Work performed (continued)", piece))
    rows.append(("Parts used", _parts_table(s["parts_used"], st)))
    if s.get("consumables_used"):
        rows.append(("Non-billable materials", _parts_table(s["consumables_used"], st)))
    return [Paragraph(f"Stop {s['stop_number']}", st["h2"]), _kv(rows, st)]


def render(report):
    _fonts()
    st = _styles()
    multi = report["date_from"] != report["date_to"]
    buf = io.BytesIO()

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("JH", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(0.75 * inch, 0.5 * inch, f"JobHub report, schema version {report['schema_version']}")
        canvas.drawRightString(letter[0] - 0.75 * inch, 0.5 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.7 * inch, bottomMargin=0.8 * inch, title="JobHub report",
                            author="JobHub")
    story = [Paragraph("JobHub Job Report", st["title"]), Spacer(1, 6)]
    period = (f"{report['date_from']} to {report['date_to']}" if multi else
              datetime.fromisoformat(report["date_from"]).strftime("%A, %Y-%m-%d"))
    gen = datetime.fromisoformat(report["generated_at"]).strftime("%Y-%m-%d %H:%M")
    head_rows = [("Period" if multi else "Date", period), ("Timezone", report["timezone"]),
                 ("Generated at", gen), ("Report schema version", report["schema_version"])]

    def summary_rows(day):
        sm = day["summary"]
        return ([("On-site time", _hm(sm["on_site_minutes"])),
                 ("Drive time", _hm(sm["drive_minutes"])),
                 ("Approximate miles", f"{sm['miles_approx']} mi (approximate)"),
                 ("Stops confirmed", str(sm["stops_confirmed"]))]
                + ([("Drive after last stop", f"{_hm(day['drive_after_last_stop']['minutes'])}, "
                     f"{day['drive_after_last_stop']['miles_approx']} mi (included in the totals above)")]
                   if day.get("drive_after_last_stop") else [])
                + ([("Work day", f"{day.get('day_start') or '—'} to {day.get('day_end') or '—'}")]
                   if day.get("day_start") or day.get("day_end") else [])
                + _end_day_rows(day.get("end_day")))

    if not multi:       # one day: the header and the day summary are a single section
        story.append(_kv(head_rows + summary_rows(report["days"][0]), st))
    else:
        story.append(_kv(head_rows, st))

    for day in report["days"]:
        d = day["date"]
        if multi:
            story.append(Paragraph(f"Day summary: {datetime.fromisoformat(d).strftime('%A, %Y-%m-%d')}", st["h1"]))
            story.append(_kv(summary_rows(day), st))
        story.extend(_mileage_table(day, st))
        if not day["stops"]:
            story.append(Spacer(1, 4))
            story.append(_p("No confirmed stops.", st["base"]))
        for s in day["stops"]:
            blk = _stop_block(s, st)
            story.extend(blk)

    story.append(Paragraph("Records to update", st["h1"]))
    head = ["Business name", "Address", "Arrival", "Departure", "Work performed", "Parts used"]
    widths = [1.1, 1.15, 0.65, 0.85, 1.85, 1.4]
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
                       _p(piece, st["small"]), _p(parts, st["small"])]
            else:  # long text continues on following rows (a table row cannot span pages)
                blank = Paragraph("", st["small"])
                row = [_p(f"(continued) {r['business_name']}", st["small"]), blank, blank, blank,
                       _p(piece, st["small"]), blank]
            if multi:
                row.insert(0, _p(r["date"], st["small"]))
            rows.append(row)
    if rows:
        story.append(_grid(head, rows, widths, st))
    else:
        story.append(_p("No confirmed stops in this period.", st["base"]))

    for day in report["days"]:
        story.extend(_recap(day, st, multi))

    story.append(KeepTogether([Paragraph("How to read this report", st["h1"])] +
                              [_p(f"{i}. {t}", st["small"]) for i, t in enumerate(INSTRUCTIONS, 1)]))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
