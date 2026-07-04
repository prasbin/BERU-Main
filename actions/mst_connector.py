"""
MST Connector — mySecondTeacher integration for IGRIS.

INVESTIGATION SUMMARY
----------------------
Checked: app.mysecondteacher.com / mysecondteacher.com / mysecondteacher.com.np,
their public Help Desk (help.mysecondteacher.com), Play Store / App Store listings,
and their tech-stack footprint.

Result: mySecondTeacher (MST) does NOT publish a developer API, OAuth integration,
webhook system, or developer portal. It's a closed student/teacher/school/parent
web + mobile platform (Advanced Pedagogy Pte. Ltd. / Innovate Tech). The only
supported access method is logging into the web app or mobile app with a
student/parent/teacher account.

CHOSEN APPROACH
----------------
Since there's no API to call, this module automates the *supported* access path
instead of guessing at private endpoints: it drives a real browser (via the
existing actions/browser_control.py, which already launches Playwright against
the user's actual installed browser profile — same engine used for go_to/get_text
elsewhere in BERU) to the MST dashboard URL, and reads the same visible page
content a logged-in user would see.

Important properties of this approach:
  - No credentials are stored, entered, or handled by this module at all.
  - It relies entirely on the user's own existing logged-in session in their real
    browser profile (cookies already on disk). If that session isn't logged in,
    this module does NOT attempt to authenticate — it reports that a manual login
    is needed and stops there.
  - It only reads what's already rendered on the page for the logged-in user —
    nothing is fetched on their behalf that they couldn't see by visiting the
    page themselves.
  - The dashboard URL is configurable (config/api_keys.json -> "mst_url") since
    the exact in-app route can change without notice on MST's end.
"""
import json
import sys
from pathlib import Path

from actions.browser_control import browser_control
from agents._llm import generate_json


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
DEFAULT_MST_URL = "https://app.mysecondteacher.com/dashboard"


EXTRACT_SYSTEM_PROMPT = """You are extracting structured assignment/deadline data from the raw
visible text of a student dashboard webpage. The text may contain navigation clutter and unrelated
UI labels — ignore that and find anything that looks like an assignment, homework, test, or
deadline with a due date.

Return ONLY valid JSON:
{
  "logged_in": true|false,
  "assignments": [
    {"title": str, "subject": str, "due_date": "YYYY-MM-DD or empty if unclear", "status": "pending|submitted|overdue|unknown"}
  ]
}

Rules:
- Set "logged_in" to false if the text looks like a login/signup/landing page rather than a
  logged-in dashboard (e.g. it shows "Login" / "Register" prompts and no personal data).
- If a due date is shown in relative or partial form (e.g. "Due tomorrow", "Due 12 Jun"), resolve
  it to YYYY-MM-DD using today's context as best you can; if genuinely unclear, leave it empty.
- Only include items that look like real assignments — never invent one.
"""


def _get_mst_url() -> str:
    try:
        cfg = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("mst_url", DEFAULT_MST_URL)
    except Exception:
        return DEFAULT_MST_URL


def sync_mst(browser: str | None = None) -> dict:
    """
    Navigates to the MST dashboard using the user's real, already-authenticated
    browser profile and extracts assignment/deadline data from the rendered page.

    Returns:
      {"ok": bool, "logged_in": bool, "assignments": list[dict], "message": str}
    """
    url = _get_mst_url()

    nav_result = browser_control(parameters={"action": "go_to", "url": url, "browser": browser})
    if nav_result.lower().startswith(("browser error", "could not start browser session")):
        return {"ok": False, "logged_in": False, "assignments": [], "message": nav_result}

    page_text = browser_control(parameters={"action": "get_text", "browser": browser})
    if not page_text or len(page_text.strip()) < 20:
        return {
            "ok": False, "logged_in": False, "assignments": [],
            "message": (
                "MST returned no readable content, Master — the page may still be loading, "
                'or the dashboard URL may need updating (config/api_keys.json -> "mst_url").'
            ),
        }

    data = generate_json(
        f"Page text:\n{page_text[:6000]}",
        model_name="gemini-2.5-flash",
        system_instruction=EXTRACT_SYSTEM_PROMPT,
    )

    logged_in   = bool(data.get("logged_in", True)) if data else False
    assignments = data.get("assignments", []) if isinstance(data, dict) else []
    assignments = [a for a in assignments if isinstance(a, dict) and a.get("title")]

    if not data or not logged_in:
        return {
            "ok": False, "logged_in": False, "assignments": [],
            "message": (
                "MST isn't logged in on this browser profile, Master. Please log in once "
                "manually — the session will persist on disk for future syncs."
            ),
        }

    return {"ok": True, "logged_in": True, "assignments": assignments, "message": "Synced."}
