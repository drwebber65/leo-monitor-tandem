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

    # get_pumper()'s "pumps" list is the one that has the right id field:
    # each pump's "assignmentId" (a UUID) is what pump_events() actually
    # wants - confirmed straight from tconnectsync's own source. The old
    # guesses (tconnectDeviceId/guid/id) don't exist on this object at all,
    # which is why the first diagnostic run never got here.
    #
    # This account has MULTIPLE pumps on file (old/retired ones included) -
    # confirmed by the second diagnostic run, which found 3 entries in
    # "pumps", two of them last uploading months ago. pumps[0] is not
    # reliably the active one, so pick whichever pump has the most recent
    # lastUploadDate (falling back to maxDateOfEvents) instead.
    device_id = None
    try:
        pumps = pumper.get("pumps") or []
        active = None
        for p in pumps:
            stamp = p.get("lastUploadDate") or p.get("maxDateOfEvents")
            if not stamp:
                continue
            if active is None or stamp > active[0]:
                active = (stamp, p)
        if active:
            device_id = active[1].get("assignmentId") or active[1].get("serialNumber")
        elif pumps:
            device_id = pumps[0].get("assignmentId") or pumps[0].get("serialNumber")
    except Exception:
        pass

    if device_id:
        print(f"Using device id: {device_id}")
        events = list(api.pump_events(device_id, min_date, max_date, True))
        print(f"Got {len(events)} events. Printing the last 20:")
        for e in events[-20:]:
            print(json.dumps(e.todict(), indent=2, default=str))

        print("\n--- Most recent event carrying an iob value (current IOB) ---")
        iob_events = [e for e in events if hasattr(e, "iob") and e.iob is not None]
        if iob_events:
            latest_iob_event = max(iob_events, key=lambda e: e.eventTimestamp)
            print(json.dumps(latest_iob_event.todict(), indent=2, default=str))
        else:
            print("No events in this window carried an iob value.")

        print("\n--- Most recent completed bolus (last bolus amount/time) ---")
        bolus_events = [
            e for e in events
            if type(e).__name__ == "LidBolusCompleted" and getattr(e, "completionStatusRaw", None) == 3
        ]
        if bolus_events:
            latest_bolus = max(bolus_events, key=lambda e: e.eventTimestamp)
            print(json.dumps(latest_bolus.todict(), indent=2, default=str))
        else:
            print("No completed boluses in this window.")
    else:
        print("Could not automatically determine a device id from the info above - "
              "look at the pumper_info()/get_pumper() output for the right field "
              "(likely something like assignmentId or serialNumber) and "
              "we'll hardcode it in the next version.")
except Exception as e:
    print(f"pump_events() failed: {e}")

print("\nDone. Copy everything above (starting from '=== pumper_info() ===') back to Claude.")
