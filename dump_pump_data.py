"""
Diagnostic script - NOT the final version.

Logs into Tandem Source using the tconnectsync library's internal API client
and prints out the raw data it gets back for pumper/device info and recent
pump events. The goal is just to SEE the actual field names Tandem uses for
things like insulin-on-board and bolus amounts, since the public docs don't
spell those out - once we see real output, this gets replaced with a script
that extracts exactly the right fields and republishes them to a Gist for
the Leo Monitor display to read.

Reads credentials from the TCONNECT_EMAIL / TCONNECT_PASSWORD environment
variables (set as GitHub Actions repo secrets - see the workflow file).
"""
import os
import sys
import json
import datetime

try:
    from tconnectsync.api.tandemsource import TandemSourceApi
except ImportError as e:
    print("ERROR: could not import tconnectsync.api.tandemsource.TandemSourceApi")
    print("This likely means tconnectsync's internal module layout has changed")
    print(f"since this script was written. Import error: {e}")
    sys.exit(1)

email = os.environ.get("TCONNECT_EMAIL")
password = os.environ.get("TCONNECT_PASSWORD")

if not email or not password:
    print("ERROR: TCONNECT_EMAIL and/or TCONNECT_PASSWORD environment variables are not set.")
    sys.exit(1)

print("Logging into Tandem Source...")
api = TandemSourceApi(email, password)
print("Login call completed (if this hung or errored above, that's the first thing to fix).")

print("\n=== pumper_info() ===")
try:
    info = api.pumper_info()
    print(json.dumps(info, indent=2, default=str))
except Exception as e:
    print(f"pumper_info() failed: {e}")

print("\n=== get_pumper() ===")
try:
    pumper = api.get_pumper()
    print(json.dumps(pumper, indent=2, default=str))
except Exception as e:
    print(f"get_pumper() failed: {e}")

print("\n=== pump_events() - last 24 hours ===")
try:
    max_date = datetime.datetime.utcnow()
    min_date = max_date - datetime.timedelta(hours=24)

    device_id = None
    try:
        pumps = info.get("pumps") or pumper.get("pumps")
        if pumps:
            device_id = pumps[0].get("tconnectDeviceId") or pumps[0].get("guid") or pumps[0].get("id")
    except Exception:
        pass

    if device_id:
        print(f"Using device id: {device_id}")
        events = list(api.pump_events(device_id, min_date, max_date, True))
        print(f"Got {len(events)} events. Printing the last 20:")
        for e in events[-20:]:
            print(json.dumps(e, indent=2, default=str))
    else:
        print("Could not automatically determine a device id from the info above - look at the pumper_info()/get_pumper() output for the right field (likely something like tconnectDeviceId, guid, or serialNumber) and we'll hardcode it in the next version.")
except Exception as e:
    print(f"pump_events() failed: {e}")

print("\nDone. Copy everything above (starting from '=== pumper_info() ===') back to Claude.")
