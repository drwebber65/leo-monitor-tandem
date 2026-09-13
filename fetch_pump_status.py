"""
Production script: logs into Tandem Source, pulls the current insulin-on-board
(IOB) and the most recent completed bolus (amount + time), and publishes just
those small values as JSON to a GitHub Gist so the Leo Monitor Tandem display
can fetch them with a plain HTTPS GET - no Tandem credentials ever touch the
ESP32.

Field names and the pump-selection logic here were confirmed against real
account data via dump_pump_data.py (see that file's comments for the full
story - short version: get_pumper()'s "pumps" list can contain old/retired
pumps, so we pick whichever one uploaded most recently; "iob" shows up on
several event types, most often LID_DAILY_BASAL, and the most recent one
across a 24h window is used as "current" IOB; the last completed bolus is
the most recent LID_BOLUS_COMPLETED event with completionStatusRaw == 3).

Required environment variables (all set as GitHub Actions repo secrets):
  TCONNECT_EMAIL, TCONNECT_PASSWORD - Tandem Source login
  GIST_TOKEN   - a GitHub Personal Access Token with "gist" scope
  GIST_ID      - the id of an existing Gist to update (create an empty one
                 first at https://gist.github.com and copy the id from its URL)
"""
import os
import sys
import json
import datetime
import urllib.request
import urllib.error

import arrow

from tconnectsync.api.tandemsource import TandemSourceApi

GIST_FILENAME = "leo-pump-status.json"


def format_event_time(ts):
    """ts is an arrow.Arrow object already in the pump's local timezone
    (tconnectsync attaches it). Returns a short, human string that fits the
    small round display - e.g. "8:45 AM" for something from today, or
    "Yest 8:45 AM" if the calendar date differs. Bolus/IOB data only ever
    comes from a 24h window, so it's never more than a day old."""
    now_local = arrow.now(ts.tzinfo)
    time_str = ts.format("h:mm A")
    if ts.date() != now_local.date():
        return f"Yest {time_str}"
    return time_str


def log(msg):
    print(msg, flush=True)


def get_credentials():
    email = os.environ.get("TCONNECT_EMAIL")
    password = os.environ.get("TCONNECT_PASSWORD")
    gist_token = os.environ.get("GIST_TOKEN")
    gist_id = os.environ.get("GIST_ID")

    missing = [
        name for name, val in [
            ("TCONNECT_EMAIL", email),
            ("TCONNECT_PASSWORD", password),
            ("GIST_TOKEN", gist_token),
            ("GIST_ID", gist_id),
        ] if not val
    ]
    if missing:
        log(f"ERROR: missing required environment variable(s): {', '.join(missing)}")
        sys.exit(1)

    return email, password, gist_token, gist_id


def pick_active_pump(pumper):
    """Pick whichever pump in get_pumper()'s "pumps" list uploaded most
    recently. This account has multiple pumps on file (old/retired ones
    included), and pumps[0] is not reliably the active one."""
    pumps = pumper.get("pumps") or []
    active = None
    for p in pumps:
        stamp = p.get("lastUploadDate") or p.get("maxDateOfEvents")
        if not stamp:
            continue
        if active is None or stamp > active[0]:
            active = (stamp, p)
    if active:
        return active[1].get("assignmentId") or active[1].get("serialNumber")
    if pumps:
        return pumps[0].get("assignmentId") or pumps[0].get("serialNumber")
    return None


def extract_status(events):
    iob_events = [e for e in events if hasattr(e, "iob") and e.iob is not None]
    current_iob = None
    iob_time = None
    if iob_events:
        latest = max(iob_events, key=lambda e: e.eventTimestamp)
        current_iob = round(float(latest.iob), 2)
        iob_time = format_event_time(latest.eventTimestamp)

    bolus_events = [
        e for e in events
        if type(e).__name__ == "LidBolusCompleted" and getattr(e, "completionStatusRaw", None) == 3
    ]
    last_bolus_units = None
    last_bolus_time = None
    if bolus_events:
        latest_bolus = max(bolus_events, key=lambda e: e.eventTimestamp)
        last_bolus_units = round(float(latest_bolus.insulinDelivered), 2)
        last_bolus_time = format_event_time(latest_bolus.eventTimestamp)

    return {
        "iob": current_iob,
        "iob_time": iob_time,
        "last_bolus_units": last_bolus_units,
        "last_bolus_time": last_bolus_time,
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


def publish_to_gist(status, gist_token, gist_id):
    body = json.dumps({
        "files": {
            GIST_FILENAME: {
                "content": json.dumps(status, indent=2)
            }
        }
    }).encode("utf-8")

    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {gist_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "leo-monitor-tandem-fetch-script",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            log(f"Gist updated ({resp.status}).")
    except urllib.error.HTTPError as e:
        log(f"ERROR: Gist update failed: HTTP {e.code}: {e.read().decode('utf-8', 'replace')}")
        sys.exit(1)


def main():
    email, password, gist_token, gist_id = get_credentials()

    log("Logging into Tandem Source...")
    api = TandemSourceApi(email, password)

    pumper = api.get_pumper()
    device_id = pick_active_pump(pumper)
    if not device_id:
        log("ERROR: could not determine an active pump id from get_pumper().")
        sys.exit(1)

    max_date = datetime.datetime.utcnow()
    min_date = max_date - datetime.timedelta(hours=24)
    events = list(api.pump_events(device_id, min_date, max_date, True))
    log(f"Fetched {len(events)} events for device {device_id}.")

    status = extract_status(events)
    log(f"Extracted status: {json.dumps(status)}")

    publish_to_gist(status, gist_token, gist_id)
    log("Done.")


if __name__ == "__main__":
    main()
