"""A US street address in parts (message format v2: the agent bills and matches on the suite).

JobHub stores one address line per place and stop ("650 Town Center Dr, Suite 100, Costa Mesa, CA 92626"). `split`
turns it into {line1, line2, city, state, zip}; `join` puts parts back into one line. Map lookups give
"street, city, state, zip" (OSM) or "street #100, city, CA 92626, USA" (Google); typed addresses vary, so anything
that does not fit leaves the whole text in line1 and the rest blank (the agent still has `formatted`).
"""
import re

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY",
    "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
CODES = set(STATES.values())
COUNTRY = re.compile(r"^(usa|us|u\.s\.a?\.?|united states( of america)?)$", re.I)
ZIP = re.compile(r"^\d{5}(-\d{4})?$")
STATE_ZIP = re.compile(r"^([A-Za-z .]+?)\s+(\d{5}(?:-\d{4})?)$")
# "650 Town Center Dr Ste 100" / "... #100" / "... Suite B-2": the unit goes to line 2
UNIT = re.compile(r"\s+((?:suite|ste\.?|unit|apt\.?|apartment|bldg\.?|building|floor|fl\.?|rm\.?|room|#)\s*[\w-]+.*)$",
                  re.I)
UNIT_ALONE = re.compile(r"^(suite|ste\.?|unit|apt\.?|apartment|bldg\.?|building|floor|fl\.?|rm\.?|room|#)\s*[\w-]+",
                        re.I)
KEYS = ("line1", "line2", "city", "state", "zip")


def _state(text):
    t = text.strip().rstrip(".")
    if t.upper() in CODES:
        return t.upper()
    return STATES.get(t.lower(), "")


def split(text):
    """'650 Town Center Dr, Suite 100, Costa Mesa, CA 92626' -> {line1, line2, city, state, zip} (each '' if unknown)."""
    out = dict.fromkeys(KEYS, "")
    segs = [p.strip() for p in (text or "").replace("\n", ",").split(",") if p.strip()]
    if segs and COUNTRY.match(segs[-1]):
        segs.pop()
    if not segs:
        return out
    # the end: "CA 92626" | "CA", "92626" | "California" | "92626"
    if segs and ZIP.match(segs[-1]):
        out["zip"] = segs.pop()
    if segs:
        m = STATE_ZIP.match(segs[-1])
        if m and _state(m.group(1)) and not out["zip"]:
            out["state"], out["zip"] = _state(m.group(1)), m.group(2)
            segs.pop()
        elif _state(segs[-1]) and len(segs) > 1:
            out["state"] = _state(segs.pop())
    if (out["state"] or out["zip"]) and len(segs) > 1:
        out["city"] = segs.pop()
    if not segs:
        return out
    out["line1"] = segs[0]
    rest = segs[1:]
    if not rest:
        m = UNIT.search(out["line1"])
        if m and m.start() > 0:
            out["line1"], rest = out["line1"][:m.start()].strip(), [m.group(1).strip()]
    elif not out["city"] and len(rest) == 1 and not UNIT_ALONE.match(rest[0]):
        out["city"], rest = rest[0], []          # "street, city" with no state or zip
    out["line2"] = ", ".join(rest)
    return out


def join(parts):
    """{line1, line2, city, state, zip} -> '650 Town Center Dr, Suite 100, Costa Mesa, CA 92626'."""
    p = {k: (parts.get(k) or "").strip() for k in KEYS}
    tail = " ".join(x for x in (p["state"], p["zip"]) if x)
    return ", ".join(x for x in (p["line1"], p["line2"], p["city"], tail) if x)
