#!/usr/bin/env python3
"""Fetch Leo's twiist status (glucose + pump) and publish it to the Gist.

Runs in GitHub Actions every 5 minutes. Logs in to the twiist insiight
follower service with a follower account, reads the latest glucose, trend,
active insulin (IOB) and last bolus, and writes them to the Gist that the
kitchen and office monitors read. No credentials ever live on the monitors.

The follower service is the one the twiist insiight iPhone app uses. It is
not an officially published API; the endpoints and login details below come
from the open-source twiistsync project (github.com/E-Allie/twiistsync).

Environment variables:
  TWIIST_USERNAME   follower account email          (required)
  TWIIST_PASSWORD   follower account password       (required)
  GIST_ID           Gist to write to                (required)
  GIST_TOKEN        GitHub token with gist scope    (required)
  GIST_FILENAME     file inside the Gist            (default leo-pump-status.json)
  TWIIST_PWD_NAME   followed person's nickname, if you follow more than one
  LOCAL_TZ          timezone for the last-bolus time (default America/New_York)

Only the Python standard library is used, so the workflow needs no pip install.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

COGNITO_URL = "https://cognito-idp.us-east-1.amazonaws.com/"
COGNITO_CLIENT_ID = "65ev2vbkr2mle7uu4cqkn7ohgl"
FOLLOWER_BASE = "https://follower-service.mytwiistportal.com"
USER_AGENT = "twiist insiight/1.0.2 CFNetwork/1568.100.1.2.3 Darwin/24.0.0"

HISTORY_HOURS = 3       # how much history the office graph shows
DELTA_MAX_GAP_S = 15 * 60

# twiist sends either a name ("fortyFiveUp") or an arrow character ("↗").
# The monitors expect Dexcom-style names, so translate here.
TREND_MAP = {
    "flat": "Flat", "→": "Flat",
    "singleUp": "SingleUp", "↑": "SingleUp",
    "doubleUp": "DoubleUp", "⇈": "DoubleUp", "↑↑": "DoubleUp",
    "fortyFiveUp": "FortyFiveUp", "↗": "FortyFiveUp",
    "singleDown": "SingleDown", "↓": "SingleDown",
    "doubleDown": "DoubleDown", "⇊": "DoubleDown", "↓↓": "DoubleDown",
    "fortyFiveDown": "FortyFiveDown", "↘": "FortyFiveDown",
}
DEXCOM_NAMES = set(TREND_MAP.values())


def http(method, url, headers=None, body=None, timeout=60):
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"Missing required environment variable {name}")
    return val


# ---------- twiist follower service ----------

def twiist_login(username, password):
    status, body = http(
        "POST", COGNITO_URL,
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        },
        body={
            "AuthFlow": "USER_PASSWORD_AUTH",
            "AuthParameters": {"USERNAME": username, "PASSWORD": password},
            "ClientId": COGNITO_CLIENT_ID,
        },
    )
    if status != 200:
        # Cognito error bodies hold an error code/message, never tokens.
        sys.exit(f"twiist login failed ({status}): {body.decode(errors='replace')[:300]}")
    result = json.loads(body)["AuthenticationResult"]
    check_follower_group(result.get("IdToken", ""))
    return result["AccessToken"]


def check_follower_group(id_token):
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return  # can't tell; let the API call decide
    if "Follower" not in (claims.get("cognito:groups") or []):
        sys.exit("This twiist login is not a follower account - use the follower "
                 "invite login, not Leo's own twiist account.")


def twiist_get(path, token):
    status, body = http(
        "GET", FOLLOWER_BASE + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "*/*",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    if status != 200:
        sys.exit(f"GET {path} failed ({status}): {body.decode(errors='replace')[:300]}")
    return json.loads(body)


def pick_person(overviews, wanted_name):
    if not overviews:
        sys.exit("Follower account isn't following anyone yet - accept the invite first.")
    names = [o.get("pwdNickname") for o in overviews]
    if wanted_name:
        for o in overviews:
            if (o.get("pwdNickname") or "").strip().lower() == wanted_name.strip().lower():
                return o
        sys.exit(f"No followed person named {wanted_name!r}. Followed: {names}")
    if len(overviews) > 1:
        print(f"Following {len(overviews)} people {names}; using the first. "
              "Set TWIIST_PWD_NAME to choose.")
    return overviews[0]


# ---------- helpers ----------

def num(v):
    """twiist sends numbers as strings ("112.0"); return float or None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_time(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def short_time(dt, tz):
    """'8:45 AM', or 'Yest 8:45 AM' / 'Mon 8:45 AM' for older boluses."""
    local = dt.astimezone(tz)
    today = datetime.now(tz).date()
    clock = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == today:
        return clock
    if local.date() == today - timedelta(days=1):
        return f"Yest {clock}"
    return f"{local.strftime('%a')} {clock}"


# ---------- Gist ----------

def gist_read(gist_id, token, filename):
    status, body = http("GET", f"https://api.github.com/gists/{gist_id}",
                        headers=gist_headers(token))
    if status != 200:
        print(f"Couldn't read the Gist ({status}); starting fresh history.")
        return {}
    files = json.loads(body).get("files", {})
    content = (files.get(filename) or {}).get("content") or "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {}


def gist_write(gist_id, token, filename, data):
    status, body = http(
        "PATCH", f"https://api.github.com/gists/{gist_id}",
        headers=gist_headers(token),
        body={"files": {filename: {"content": json.dumps(data, indent=1)}}},
    )
    if status != 200:
        sys.exit(f"Gist update failed ({status}): {body.decode(errors='replace')[:300]}")


def gist_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "leo-monitor-twiist",
    }


# ---------- main ----------

def build_status(person, details, previous, tz, now):
    status = person.get("status") or {}
    summary = status.get("summary") or {}

    glucose = num(summary.get("glucoseQuantity"))
    unit = (summary.get("glucoseUnit") or "mg/dL").lower()
    if glucose is not None and unit.startswith("mmol"):
        glucose = glucose * 18.0  # monitors work in mg/dL
    g_time = parse_time(summary.get("glucoseDate"))
    g_epoch = int(g_time.timestamp()) if g_time else None

    raw_arrow = summary.get("cgmRateArrow") or ""
    trend = TREND_MAP.get(raw_arrow) or (raw_arrow if raw_arrow in DEXCOM_NAMES else "")

    # History for the office graph and the delta: [[epoch, mg/dL], ...] newest first.
    history = [h for h in (previous.get("history") or [])
               if isinstance(h, list) and len(h) == 2]
    if glucose is not None and g_epoch and (not history or history[0][0] != g_epoch):
        history.insert(0, [g_epoch, round(glucose)])
    cutoff = int(now.timestamp()) - HISTORY_HOURS * 3600 - 600
    history = sorted({h[0]: h for h in history if h[0] >= cutoff}.values(),
                     key=lambda h: h[0], reverse=True)

    delta = None
    if len(history) > 1 and history[0][0] - history[1][0] <= DELTA_MAX_GAP_S:
        delta = history[0][1] - history[1][1]

    iob = num(details.get("activeInsulin_Units"))
    bolus_units = num(details.get("lastBolusVolume_Units"))
    bolus_time = parse_time(details.get("lastBolusDate"))

    return {
        "updated": now.isoformat(timespec="seconds"),
        "glucose": round(glucose) if glucose is not None else None,
        "glucose_epoch": g_epoch,
        "trend": trend,
        "delta": delta,
        "iob": round(iob, 2) if iob is not None else None,
        "last_bolus_units": round(bolus_units, 2) if bolus_units is not None else None,
        "last_bolus_time": short_time(bolus_time, tz) if bolus_time else "",
        "last_bolus_epoch": int(bolus_time.timestamp()) if bolus_time else None,
        "history": history,
    }


def main():
    username = env("TWIIST_USERNAME", required=True)
    password = env("TWIIST_PASSWORD", required=True)
    gist_id = env("GIST_ID", required=True)
    gist_token = env("GIST_TOKEN", required=True)
    filename = env("GIST_FILENAME", "leo-pump-status.json")
    tz = ZoneInfo(env("LOCAL_TZ", "America/New_York"))

    token = twiist_login(username, password)
    person = pick_person(twiist_get("/pwd/overviews", token), env("TWIIST_PWD_NAME"))

    # IOB and last bolus live in status.details. The overview usually has
    # only the summary, so fall back to the full package for details.
    details = (person.get("status") or {}).get("details")
    if not details:
        pwd_id = str(person.get("pwdId", "")).lower()
        package = twiist_get(f"/pwd/{pwd_id}/package", token)
        details = (package.get("status") or {}).get("details") or {}

    now = datetime.now(timezone.utc)
    previous = gist_read(gist_id, gist_token, filename)
    data = build_status(person, details, previous, tz, now)
    gist_write(gist_id, gist_token, filename, data)

    age = (int(now.timestamp()) - data["glucose_epoch"]) // 60 if data["glucose_epoch"] else None
    print(f"glucose={data['glucose']} trend={data['trend']} delta={data['delta']} "
          f"age_min={age} iob={data['iob']} last_bolus={data['last_bolus_units']} "
          f"@ {data['last_bolus_time']} history={len(data['history'])}")


if __name__ == "__main__":
    main()
