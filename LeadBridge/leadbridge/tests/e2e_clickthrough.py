"""End-to-end browser click-through of LeadBridge, driven like a real user.

Self-resetting: it reseeds the demo data and forces the mock CRM back into
"outage" before it starts, and restores that state when it finishes, so it can
be run repeatedly without manual cleanup.

Requires both servers running:
    PYTHONPATH=. uvicorn app.main:app --port 8010
    npm run dev
Then:  python tests/e2e_clickthrough.py
"""

import os
import sys

import httpx
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sqlmodel import Session, delete  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.models import Lead  # noqa: E402
from seed import seed  # noqa: E402

URL = "http://localhost:5175"
API = "http://localhost:8010"
SHOTS = os.path.join(os.path.dirname(__file__), "screenshots")
os.makedirs(SHOTS, exist_ok=True)

console_errors = []
failures = []


def reset_state():
    """Put the demo back to its known starting point: fresh seed + CRM in outage."""
    init_db()
    with Session(engine) as s:
        s.exec(delete(Lead))
        s.commit()
    seed()
    if httpx.get(f"{API}/mock-crm").json()["recovered"]:
        httpx.post(f"{API}/mock-crm/toggle")


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail else ""))
    if not cond:
        failures.append(label)


reset_state()

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(str(e)))

    # ---------- 1. Page loads ----------
    page.goto(URL, wait_until="networkidle")
    check("page loads with title", "LeadBridge" in page.title(), page.title())
    check("headline renders", page.locator("h1").inner_text() == "LeadBridge")
    check("CRM banner shows outage", "simulating outage" in page.locator(".crm-banner").inner_text())
    page.screenshot(path=f"{SHOTS}/01_initial.png")

    # ---------- 2. Default Failed view shows the dead-letter queue ----------
    rows = page.locator(".leads-table tbody tr")
    check("failed view lists 2 dead-lettered leads", rows.count() == 2, f"count={rows.count()}")
    check("dead letter shows error text", "simulated outage" in rows.first.inner_text())
    check("replay buttons present", page.locator(".replay-btn").count() == 2)

    # ---------- 3. Every filter tab ----------
    for name, expected in [("all", 5), ("delivered", 3), ("received", 0), ("failed", 2)]:
        page.get_by_role("button", name=name, exact=True).click()
        page.wait_for_timeout(600)
        n = page.locator(".leads-table tbody tr").count()
        if expected == 0:
            empty = page.locator(".empty-state").count() == 1
            check(f"filter '{name}' shows empty state", empty)
        else:
            check(f"filter '{name}' shows {expected} rows", n == expected, f"got {n}")
    page.screenshot(path=f"{SHOTS}/02_filters.png")

    # ---------- 4. Refresh button ----------
    page.get_by_role("button", name="Refresh").click()
    page.wait_for_timeout(600)
    check("refresh keeps failed view intact", page.locator(".leads-table tbody tr").count() == 2)

    # ---------- 5. Submit a GOOD lead through the form ----------
    page.get_by_placeholder("Name").fill("Nadia Osman")
    page.get_by_placeholder("Email", exact=False).fill("nadia@goodcorp.com")
    page.get_by_role("button", name="Submit test lead").click()
    page.wait_for_timeout(2500)
    check("good lead shows confirmation notice", page.locator(".notice-banner").count() == 1,
          page.locator(".notice-banner").inner_text() if page.locator(".notice-banner").count() else "no banner")
    check("form cleared after submit", page.get_by_placeholder("Name").input_value() == "")
    page.get_by_role("button", name="delivered", exact=True).click()
    page.wait_for_timeout(800)
    check("good lead appears as delivered", "nadia@goodcorp.com" in page.locator(".leads-table").inner_text())
    page.screenshot(path=f"{SHOTS}/03_good_lead_delivered.png")

    # ---------- 6. Submit a FAILING lead -> must dead-letter ----------
    page.get_by_placeholder("Name").fill("Tomas Lindqvist")
    page.get_by_placeholder("Email", exact=False).fill("tomas@fail.dev")
    page.get_by_role("button", name="Submit test lead").click()
    page.wait_for_timeout(3500)
    page.get_by_role("button", name="failed", exact=True).click()
    page.wait_for_timeout(900)
    table = page.locator(".leads-table").inner_text()
    check("failing lead dead-lettered into failed queue", "tomas@fail.dev" in table)
    check("failed queue now has 3", page.locator(".leads-table tbody tr").count() == 3)
    page.screenshot(path=f"{SHOTS}/04_dead_lettered.png")

    # ---------- 7. Replay while CRM still down -> stays failed ----------
    page.locator(".replay-btn").first.click()
    page.wait_for_timeout(3000)
    notice = page.locator(".notice-banner").inner_text()
    check("replay during outage reports still-failing", "still failing" in notice, notice)
    check("lead remains in dead-letter queue", page.locator(".leads-table tbody tr").count() == 3)
    page.screenshot(path=f"{SHOTS}/05_replay_still_failing.png")

    # ---------- 8. Simulate CRM recovery, replay -> delivered ----------
    page.get_by_role("button", name="Simulate CRM recovery").click()
    page.wait_for_timeout(700)
    check("banner flips to healthy", "healthy" in page.locator(".crm-banner").inner_text())

    before = page.locator(".leads-table tbody tr").count()
    page.locator(".replay-btn").first.click()
    page.wait_for_timeout(2500)
    notice = page.locator(".notice-banner").inner_text()
    check("replay after recovery reports delivered", "delivered to CRM" in notice, notice)
    after = page.locator(".leads-table tbody tr").count()
    check("recovered lead leaves the dead-letter queue", after == before - 1, f"{before} -> {after}")
    page.screenshot(path=f"{SHOTS}/06_replayed_delivered.png")

    # ---------- 9. Drain the rest of the queue via Replay ----------
    guard = 0
    while page.locator(".replay-btn").count() > 0 and guard < 5:
        page.locator(".replay-btn").first.click()
        page.wait_for_timeout(2000)
        guard += 1
    check("entire dead-letter queue drained by replay", page.locator(".empty-state").count() == 1)
    page.screenshot(path=f"{SHOTS}/07_queue_empty.png")

    page.get_by_role("button", name="delivered", exact=True).click()
    page.wait_for_timeout(800)
    check("all leads now delivered", page.locator(".leads-table tbody tr").count() == 7,
          f"count={page.locator('.leads-table tbody tr').count()}")
    page.screenshot(path=f"{SHOTS}/08_all_delivered.png")

    # ---------- 10. Honeypot: bot-style submission must be dropped ----------
    page.evaluate("""() => {
      const hp = document.querySelector('.honeypot');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(hp, 'http://spam-bot.example');
      hp.dispatchEvent(new Event('input', { bubbles: true }));
    }""")
    page.get_by_placeholder("Name").fill("Spam Bot")
    page.get_by_placeholder("Email", exact=False).fill("bot@spammer.com")
    page.get_by_role("button", name="Submit test lead").click()
    page.wait_for_timeout(2500)
    page.get_by_role("button", name="all", exact=True).click()
    page.wait_for_timeout(900)
    check("honeypot submission silently dropped", "bot@spammer.com" not in page.locator(".leads-table").inner_text())
    page.screenshot(path=f"{SHOTS}/09_honeypot_dropped.png")

    # ---------- 11. Client-side validation ----------
    page.get_by_placeholder("Name").fill("No Email Person")
    page.get_by_placeholder("Email", exact=False).fill("")
    page.get_by_role("button", name="Submit test lead").click()
    page.wait_for_timeout(700)
    check("empty email blocked by form validation",
          page.get_by_placeholder("Name").input_value() == "No Email Person")

    check("no console errors during whole run", len(console_errors) == 0, str(console_errors[:3]))
    browser.close()

# leave the demo exactly as a first-time visitor should find it
reset_state()

print("\n" + "=" * 60)
if failures:
    print(f"FAILURES ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("ALL BROWSER CHECKS PASSED")
