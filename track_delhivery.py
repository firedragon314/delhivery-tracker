"""
Delhivery parcel tracker + SMS notifier.

What it does
------------
1. Opens Delhivery's public tracking page in a headless browser (the page
   is JS-rendered, so plain requests/BeautifulSoup won't see the content).
2. Searches for your waybill (AWB) number.
3. Pulls out the latest status line and the tentative delivery date.
4. If the status is NEW since the last run, sends you an SMS via Twilio:
       {time of update} + {update} + tentative date is {tentative date}
5. Remembers the last status in last_status.json so you don't get the
   same SMS twice.

IMPORTANT — one-time setup step
--------------------------------
Delhivery's page layout isn't something I can guarantee matches this
script exactly (it's a JS single-page app and can change without notice).
Before you trust this on autopilot:

    python track_delhivery.py --debug

This prints the full text the browser sees, plus what the script
extracted as "status" and "tentative_date". If those look wrong, adjust
the regex patterns in `extract_status_and_date()` to match what you see
in the debug dump — it's usually a 2-minute fix.

Environment variables required
-------------------------------
WAYBILL       Your Delhivery tracking / AWB number
TO_PHONE      Your phone number in E.164 format, e.g. +9198XXXXXXXX
TWILIO_SID    Twilio Account SID
TWILIO_AUTH   Twilio Auth Token
TWILIO_FROM   Your Twilio phone number (the "from" number Twilio gave you)
"""

import os
import re
import sys
import json
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
from twilio.rest import Client

IST = timezone(timedelta(hours=5, minutes=30))
STATE_FILE = "last_status.json"
TRACK_PAGE = "https://www.delhivery.com/tracking"


def fetch_tracking_text(waybill: str) -> str:
    """Loads the Delhivery tracking page, submits the waybill, returns
    all visible text on the results view."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(TRACK_PAGE, timeout=60000)
        page.wait_for_timeout(3000)  # let the SPA hydrate

        # The tracking input is usually the first visible text/search box.
        # We try a few common selectors before giving up.
        input_box = None
        for selector in [
            "input[type='search']",
            "input[type='text']",
            "input[placeholder*='AWB' i]",
            "input[placeholder*='track' i]",
            "input",
        ]:
            locator = page.locator(selector).first
            if locator.count() > 0:
                input_box = locator
                break

        if input_box is None:
            browser.close()
            raise RuntimeError(
                "Could not find the tracking search box. Delhivery's page "
                "layout may have changed — run with --debug and inspect "
                "the HTML manually."
            )

        input_box.fill(waybill)
        input_box.press("Enter")
        page.wait_for_timeout(6000)  # let results render

        body_text = page.inner_text("body")
        browser.close()
    return body_text


def extract_status_and_date(body_text: str):
    """Heuristic text-mining of the rendered page. Adjust the regexes
    below if your debug dump shows a different phrasing/layout."""
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]

    status = None
    tentative_date = None

    date_pattern = r"\d{1,2}[-\s]?\w{3,9}[-\s]?\d{2,4}"

    for i, line in enumerate(lines):
        if re.search(r"(expected|tentative|estimated).{0,20}(delivery|date)", line, re.I):
            m = re.search(date_pattern, line)
            if m:
                tentative_date = m.group(0)
            elif i + 1 < len(lines) and re.search(date_pattern, lines[i + 1]):
                tentative_date = lines[i + 1]

    status_keywords = (
        r"(picked up|in transit|out for delivery|delivered|reached|"
        r"dispatched|shipment (created|booked)|pending|arrived|"
        r"undelivered|attempted)"
    )
    for line in lines:
        if re.search(status_keywords, line, re.I):
            status = line
            break

    return status or "Status not found (check --debug output)", tentative_date or "Not available yet"


def load_last_status():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f).get("status")
    return None


def save_last_status(status: str):
    with open(STATE_FILE, "w") as f:
        json.dump({"status": status, "checked_at": datetime.now(IST).isoformat()}, f)


def send_sms(update: str, tentative_date: str):
    now_str = datetime.now(IST).strftime("%d-%b %I:%M %p")
    message = f"{now_str} + {update} + tentative date is {tentative_date}"

    sid = os.environ["TWILIO_SID"]
    auth = os.environ["TWILIO_AUTH"]
    from_number = os.environ["TWILIO_FROM"]
    to_number = os.environ["TO_PHONE"]

    client = Client(sid, auth)
    client.messages.create(body=message, from_=from_number, to=to_number)
    print("SMS sent:", message)


def main():
    debug = "--debug" in sys.argv
    waybill = os.environ["WAYBILL"]

    body_text = fetch_tracking_text(waybill)

    if debug:
        print("=" * 60)
        print("RAW PAGE TEXT (first 3000 chars):")
        print("=" * 60)
        print(body_text[:3000])
        print("=" * 60)

    status, tentative_date = extract_status_and_date(body_text)

    if debug:
        print(f"Extracted status:         {status}")
        print(f"Extracted tentative date: {tentative_date}")
        print("If these look wrong, edit extract_status_and_date().")
        return  # don't send SMS during a debug run

    last_status = load_last_status()

    if last_status is None:
        print("First check ever recorded — sending initial alert SMS.")
    elif status == last_status:
        print("No change in status since last check. Skipping SMS.")
        return
    else:
        print(f"Status changed: '{last_status}' -> '{status}'. Sending SMS.")

    send_sms(status, tentative_date)
    save_last_status(status)


if __name__ == "__main__":
    main()
