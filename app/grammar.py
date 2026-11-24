"""Optional grammar check for work notes through a self-hosted LanguageTool server (LANGUAGETOOL_URL,
blank = off). Only the note text is sent, to the address you configured; nothing else leaves the server."""
import requests

LANGUAGE = "en-US"
MAX_CHARS = 5000
MAX_ISSUES = 30


class GrammarError(Exception):
    pass


def check(cfg, text):
    """Return a list of {offset, length, message, replacements, rule}. Raises GrammarError."""
    url = (cfg.languagetool_url or "").strip().rstrip("/")
    if not url:
        raise GrammarError("Grammar check is off. Set the LanguageTool address in Settings.")
    text = (text or "")[:MAX_CHARS]
    if not text.strip():
        return []
    try:
        r = requests.post(f"{url}/v2/check", data={"text": text, "language": LANGUAGE}, timeout=8)
        r.raise_for_status()
        matches = r.json().get("matches", [])
    except Exception:
        raise GrammarError("Could not reach the grammar server.")
    out = []
    for m in matches[:MAX_ISSUES]:
        out.append({"offset": m.get("offset", 0), "length": m.get("length", 0),
                    "message": m.get("message", ""),
                    "replacements": [x.get("value", "") for x in m.get("replacements", [])[:3]],
                    "rule": (m.get("rule") or {}).get("id", "")})
    return out


def test(url):
    """Send a fixed sentence with a known mistake to the server; returns (ok, message). Uses no real notes."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return False, "Enter the LanguageTool address first."
    try:
        r = requests.post(f"{url}/v2/check", data={"text": "This are a test of the grammar check.",
                                                   "language": LANGUAGE}, timeout=8)
        r.raise_for_status()
        n = len(r.json()["matches"])
    except Exception:
        return False, "Could not reach LanguageTool at that address (check the IP, port and that the container is running)."
    if not n:
        return False, "Connected, but it found no mistake in a sentence that has one. Is this really LanguageTool?"
    return True, "LanguageTool works (it flagged the mistake in the test sentence)."
