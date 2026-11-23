"""AI note polishing with Claude (Anthropic API). Optional and off until a key is set (Settings → AI).

Only the Work performed text is sent: no customer name, address, location or anything else. The result is a
suggestion: the stop sheet shows it next to the original and nothing changes until "Use it" is tapped.

Model: Claude Opus 5.5 at low effort (proofreading needs little thinking). Server-side fallback is on: if Claude
declines a request for a safety reason, the API retries it on a fallback model inside the same call. Each call is
counted per month (`api_usage`, provider "ai") and stops at AI_MONTHLY_CAP.
"""
from datetime import datetime, timezone

from .errors import EditError

MODEL = "claude-opus-5-5"
MAX_CHARS = 5000

SYSTEM = """You proofread a field service technician's job notes ("work performed"). The notes go into a daily report \
and a customer record, so they must read cleanly and stay exactly as true as the original.

Do:
- Fix spelling, grammar, punctuation and capitalization.
- Turn shorthand and fragments into short, clear sentences in plain English.
- Keep the technician's voice: first person or plain statements, past tense for work done.
- Keep the original order and any line breaks or list structure.

Never:
- Add, remove or guess facts, causes, recommendations or outcomes.
- Change any number, measurement, time, quantity, part name, model or serial number, or product name.
- Add greetings, headings, sign-offs or commentary.

Reply with the revised notes only. If the notes are already clean, return them unchanged."""


class AIError(Exception):
    pass


def enabled(cfg):
    return bool(cfg.anthropic_key) and cfg.ai_notes == "on"


def _client(cfg):
    import anthropic                      # imported here so the app runs without the package when AI is off
    return anthropic.Anthropic(api_key=cfg.anthropic_key, timeout=60.0, max_retries=2)


def _count(conn, cap):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    conn.execute("BEGIN IMMEDIATE")
    used = conn.execute("SELECT COALESCE(SUM(count),0) FROM api_usage WHERE provider='ai' AND day LIKE ?",
                        (month + "%",)).fetchone()[0]
    if used >= cap:
        conn.rollback()
        raise EditError(f"Monthly AI limit reached ({cap}). Change it in Settings → AI.")
    day = datetime.now(timezone.utc).date().isoformat()
    conn.execute("INSERT INTO api_usage(day, provider, count) VALUES (?, 'ai', 1) "
                 "ON CONFLICT(day, provider) DO UPDATE SET count=count+1", (day,))
    conn.commit()


def polish(conn, cfg, text, client=None):
    """Return the proofread notes (a suggestion). Raises EditError with a message for the sheet."""
    if not enabled(cfg):
        raise EditError("AI notes are off. Add an Anthropic API key in Settings → AI.")
    text = (text or "").strip()
    if not text:
        raise EditError("Write some notes first.")
    if len(text) > MAX_CHARS:
        raise EditError(f"The notes are too long to check at once ({MAX_CHARS} characters at most).")
    _count(conn, cfg.ai_monthly_cap)
    import anthropic
    client = client or _client(cfg)
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                         # a declined request is retried on a fallback model
            output_config={"effort": "low"},
            system=SYSTEM,
            messages=[{"role": "user", "content": "<notes>\n" + text + "\n</notes>"}],
        )
    except anthropic.AuthenticationError:
        raise EditError("The Anthropic API key was refused. Check it in Settings → AI.")
    except anthropic.PermissionDeniedError:
        raise EditError("This Anthropic key is not allowed to use the model. Check the account in the Anthropic Console.")
    except anthropic.RateLimitError:
        raise EditError("Too many requests right now (or the account's spend limit was reached). Try again in a minute.")
    except anthropic.APIStatusError as e:
        raise EditError(f"The AI service answered {e.status_code}. Try again later.")
    except anthropic.APIConnectionError:
        raise EditError("Could not reach the AI service. Check the server's internet connection.")
    if resp.stop_reason == "refusal":
        raise EditError("The AI declined to edit these notes. Keep yours.")
    out = "".join(b.text for b in resp.content if b.type == "text").strip()
    if out.startswith("<notes>"):
        out = out[len("<notes>"):]
    if out.endswith("</notes>"):
        out = out[:-len("</notes>")]
    out = out.strip()
    if not out:
        raise EditError("The AI returned nothing. Keep yours.")
    if resp.stop_reason == "max_tokens":
        raise EditError("The AI's answer was cut off. Try a shorter piece.")
    return out
