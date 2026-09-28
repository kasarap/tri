#!/usr/bin/env python3
"""
refresh_dashboard.py

Pulls fresh training data from Garmin Connect + Strava, computes a
TrainingPeaks-style CTL/ATL/TSB model from Strava's Relative Effort,
and regenerates the static dashboard HTML (dashboard_template.html ->
OUTPUT_PATH) with the data baked in as JSON.

No TrainingPeaks account/API access is used or required — CTL/ATL/TSB
are modeled locally so this script has no TrainingPeaks dependency at all.

Environment variables (set these in run_refresh.sh or your cron/CI secrets):
  ARC_RELAY_URL                   - base URL for your Arc Relay instance's
                                    internal listener, e.g.
                                    http://127.0.0.1:8103 (requires
                                    run_refresh.sh's --network host so this
                                    container can reach that port)
  ARC_RELAY_API_KEY               - a static API key generated from Arc
                                    Relay's own "API Keys" admin page.
                                    Garmin data is pulled through your
                                    existing Arc-Relay-hosted
                                    garmin-connect-mcp server over this,
                                    instead of logging into Garmin
                                    independently — reuses a session that's
                                    already been through MFA once, rather
                                    than re-fighting Garmin's login/MFA/
                                    rate-limit wall on every run. If that
                                    session ever logs out, re-run your usual
                                    garmin-connect-mcp interactive auth flow
                                    and it starts working again with no
                                    changes needed here.
  STRAVA_CLIENT_ID
  STRAVA_CLIENT_SECRET
  STRAVA_REFRESH_TOKEN            - a long-lived Strava OAuth refresh token
  RACE_NAME   (optional, default: "Ironman 70.3 Mallorca (Alcúdia)")
  RACE_DATE   (optional, default: "2027-05-08", format YYYY-MM-DD)
  OUTPUT_PATH (optional, default: "./index.html")
  TEMPLATE_PATH (optional, default: "./dashboard_template.html")

Usage:
  python3 refresh_dashboard.py
"""
import os
import sys
import json
import math
import datetime as dt

import requests


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ARC_RELAY_URL = os.environ.get("ARC_RELAY_URL", "http://127.0.0.1:8103").rstrip("/")
ARC_RELAY_API_KEY = os.environ.get("ARC_RELAY_API_KEY")

STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN")

RACE_NAME = os.environ.get("RACE_NAME", "Ironman 70.3 Mallorca (Alcúdia)")
RACE_DATE = os.environ.get("RACE_DATE", "2027-05-08")

OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "./index.html")
TEMPLATE_PATH = os.environ.get("TEMPLATE_PATH", "./dashboard_template.html")

GARMIN_HISTORY_DAYS = int(os.environ.get("GARMIN_HISTORY_DAYS", "180"))
STRAVA_HISTORY_DAYS = int(os.environ.get("STRAVA_HISTORY_DAYS", "180"))
LOAD_CAP_HOURS = float(os.environ.get("LOAD_CAP_HOURS", "4.0"))  # guard vs. forgotten-stopped-watch entries

# Performance thresholds — set these to your own numbers via env vars.
# These drive the zone tables; they are not auto-detected.
THRESHOLDS = {
    "ftp_watts": float(os.environ.get("FTP_WATTS", "204")),
    "lthr_bike": int(os.environ.get("LTHR_BIKE", "172")),
    "lthr_run": int(os.environ.get("LTHR_RUN", "172")),
    "threshold_pace_min_per_mi": float(os.environ.get("THRESHOLD_PACE_MIN_PER_MI", "7.87")),  # 7:52/mi
    "swim_css_min_per_100y": float(os.environ.get("SWIM_CSS_MIN_PER_100Y", "2.47")),  # 2:28/100yd-ish
    "max_hr": int(os.environ.get("MAX_HR", "190")),
}

CTL_TIME_CONSTANT = 42.0
ATL_TIME_CONSTANT = 7.0


def log(msg):
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Arc Relay MCP client (used for Garmin data)
# ---------------------------------------------------------------------------
def call_mcp_tool(server, tool_name, arguments, request_id=1):
    """Minimal synchronous JSON-RPC client for an Arc-Relay-hosted MCP
    server. Arc Relay's HTTP transport here responds with a single JSON
    object per call (no SSE stream to manage), so this is just a POST.
    """
    if not ARC_RELAY_API_KEY:
        raise RuntimeError("ARC_RELAY_API_KEY is not set")

    resp = requests.post(
        f"{ARC_RELAY_URL}/mcp/{server}",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ARC_RELAY_API_KEY}",
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        timeout=150,
    )
    resp.raise_for_status()
    payload = resp.json()

    if "error" in payload:
        raise RuntimeError(f"Arc Relay JSON-RPC error calling {tool_name}: {payload['error']}")

    result = payload["result"]
    text = result["content"][0]["text"]

    if result.get("isError"):
        raise RuntimeError(
            f"{server}/{tool_name} returned an error: {text}\n"
            "(If this says something like 'authenticate interactively', the "
            "Garmin session behind Arc Relay has logged out — run your usual "
            "garmin-connect-mcp interactive auth flow to fix it; nothing in "
            "this script needs to change.)"
        )

    return json.loads(text)


# ---------------------------------------------------------------------------
# Garmin (pulled through Arc Relay's already-authenticated garmin-connect-mcp,
# not via an independent login — see call_mcp_tool above and the module
# docstring for why)
# ---------------------------------------------------------------------------
def fetch_garmin(days):
    end_date = dt.date.today()
    start_date = end_date - dt.timedelta(days=days)

    daily_by_date = {}
    cursor = None
    page = 0

    while True:
        page += 1
        args = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "limit": 30,
            "include_training_readiness": True,
            "include_training_status": True,
            "include_body_battery": True,
        }
        if cursor:
            args["cursor"] = cursor

        log(f"Garmin (via Arc Relay): fetching health summary page {page}...")
        data = call_mcp_tool("garmin-connect", "query_health_summary", args)

        summaries = data["data"]["summaries"]
        for s in summaries:
            ds = s["date"]["date"]
            us = s.get("user_summary") or {}
            tr_list = s.get("training_readiness") or []
            tr = tr_list[-1] if tr_list else {}
            daily_by_date[ds] = {
                "date": ds,
                "restingHR": us.get("restingHeartRate"),
                "avgStress": us.get("averageStressLevel"),
                "steps": us.get("totalSteps"),
                "bbHigh": us.get("bodyBatteryHighestValue"),
                "bbLow": us.get("bodyBatteryLowestValue"),
                "totalKcal": us.get("totalKilocalories"),
                "activeKcal": us.get("activeKilocalories"),
                "bmrKcal": us.get("bmrKilocalories"),
                "readiness": tr.get("score"),
                "sleepScore": tr.get("sleepScore"),
                "hrvWeekly": tr.get("hrvWeeklyAverage"),
                "_training_status": s.get("training_status"),
            }

        pagination = data.get("pagination") or {}
        if pagination.get("has_more") and pagination.get("cursor"):
            cursor = pagination["cursor"]
        else:
            break

    # VO2max trend: pull from the same per-day training_status blocks,
    # keeping only points where the value actually changed.
    vo2max = []
    last_vo2 = None
    for ds in sorted(daily_by_date.keys()):
        ts = daily_by_date[ds].pop("_training_status", None) or {}
        vo2 = (ts.get("mostRecentVO2Max") or {}).get("generic") or {}
        v = vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue")
        vdate = vo2.get("calendarDate")
        if v and vdate and v != last_vo2:
            vo2max.append({"date": vdate, "value": v})
            last_vo2 = v

    daily = [daily_by_date[ds] for ds in sorted(daily_by_date.keys())]
    return daily, vo2max


# ---------------------------------------------------------------------------
# Strava
# ---------------------------------------------------------------------------
def strava_access_token():
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": STRAVA_REFRESH_TOKEN,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_strava_activities(days):
    token = strava_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    after = int((dt.datetime.now() - dt.timedelta(days=days)).timestamp())

    activities = []
    page = 1
    while True:
        resp = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers=headers,
            params={"after": after, "per_page": 200, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        activities.extend(batch)
        page += 1
        if len(batch) < 200:
            break

    log(f"Strava: fetched {len(activities)} activities")
    return activities


def bucket_sport(sport_type):
    if sport_type in ("Run", "VirtualRun", "TrailRun"):
        return "Run"
    if sport_type in ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide"):
        return "Bike"
    if sport_type in ("Swim",):
        return "Swim"
    return "Other"


def monday_of(date_obj):
    return (date_obj - dt.timedelta(days=date_obj.weekday())).isoformat()


def daily_load_from_activity(a):
    """Estimate a TSS-like daily training load for one activity.

    Prefers Strava's suffer_score (Relative Effort) when present, since
    that's already a heart-rate-based training-load metric. Falls back to
    a duration x intensity heuristic (avg HR relative to max) when
    suffer_score is unavailable (e.g. no HR strap for that activity).
    """
    moving_hours = min(a.get("moving_time", 0) / 3600.0, LOAD_CAP_HOURS)
    suffer = a.get("suffer_score")
    if suffer:
        return min(suffer, LOAD_CAP_HOURS * 100)  # cap alongside the duration cap
    avg_hr = a.get("average_heartrate")
    if avg_hr:
        # crude HR-based load: intensity factor squared x hours x 100,
        # using ~145bpm as a rough "threshold-ish" reference effort
        intensity = min(avg_hr / 145.0, 1.4)
        return round(moving_hours * (intensity ** 2) * 100, 1)
    # no HR at all (e.g. some strength sessions): flat load per hour
    return round(moving_hours * 40, 1)


def compute_weekly_hours(activities):
    weekly = {}
    for a in activities:
        try:
            start = dt.datetime.fromisoformat(a["start_date_local"].replace("Z", ""))
        except Exception:
            continue
        wk = monday_of(start.date())
        bucket = bucket_sport(a.get("sport_type") or a.get("type"))
        hours = min(a.get("moving_time", 0) / 3600.0, LOAD_CAP_HOURS)
        row = weekly.setdefault(wk, {"week": wk, "Swim": 0.0, "Bike": 0.0, "Run": 0.0, "Other": 0.0})
        row[bucket] = round(row[bucket] + hours, 2)
    return [weekly[k] for k in sorted(weekly.keys())]


def build_daily_load(activities):
    """Shared daily training-load map, keyed by ISO date, used by both the
    CTL/ATL/TSB model and the ACWR/monotony/strain calculations."""
    daily_load = {}
    for a in activities:
        try:
            start = dt.datetime.fromisoformat(a["start_date_local"].replace("Z", ""))
        except Exception:
            continue
        d = start.date().isoformat()
        daily_load[d] = daily_load.get(d, 0.0) + daily_load_from_activity(a)
    return daily_load


def compute_ctl_atl_tsb(daily_load, history_days):
    """Standard exponentially-weighted CTL(42d)/ATL(7d)/TSB model,
    driven by a daily summed training-load proxy from Strava."""
    today = dt.date.today()
    start_date = today - dt.timedelta(days=history_days)

    ctl = atl = 0.0
    # seed with a short warm-up before the visible window so early days aren't flat zero
    seed_start = start_date - dt.timedelta(days=14)
    d = seed_start
    series = []
    while d <= today:
        ds = d.isoformat()
        tss = daily_load.get(ds, 0.0)
        ctl += (tss - ctl) / CTL_TIME_CONSTANT
        atl += (tss - atl) / ATL_TIME_CONSTANT
        if d >= start_date:
            series.append({"date": ds, "tss": round(tss, 1), "ctl": round(ctl, 1),
                            "atl": round(atl, 1), "tsb": round(ctl - atl, 1)})
        d += dt.timedelta(days=1)

    # weekly snapshot (last value of each week), matching the dashboard's chart granularity
    weekly = {}
    for row in series:
        wk = monday_of(dt.date.fromisoformat(row["date"]))
        w = weekly.setdefault(wk, {"week": wk, "tss": 0.0, "ctl": None, "atl": None, "tsb": None, "last": None})
        w["tss"] += row["tss"]
        if w["last"] is None or row["date"] > w["last"]:
            w["last"] = row["date"]
            w["ctl"], w["atl"], w["tsb"] = row["ctl"], row["atl"], row["tsb"]
    tp_weekly = [
        {"week": k, "tss": round(v["tss"], 1), "ctl": v["ctl"], "atl": v["atl"], "tsb": v["tsb"]}
        for k, v in sorted(weekly.items())
    ]

    current = series[-1] if series else {"ctl": 0, "atl": 0, "tsb": 0}
    tsb = current["tsb"]
    status = "Fresh (tapered)" if tsb > 15 else "Tired (absorbing training)" if tsb < -10 else "Balanced"
    current_fitness = {"ctl": current["ctl"], "atl": current["atl"], "tsb": current["tsb"], "fitness_status": status}

    return tp_weekly, current_fitness


def compute_load_balance(daily_load):
    """ACWR (acute:chronic workload ratio), Monotony, and weekly Strain —
    the same Foster/Gabbett-style load-management metrics athletedata.health
    (and TrainingPeaks' own PMC) surface, computed the same standard way."""
    today = dt.date.today()
    last28 = [daily_load.get((today - dt.timedelta(days=i)).isoformat(), 0.0) for i in range(27, -1, -1)]
    last7 = last28[-7:]

    acute = sum(last7) / 7.0
    chronic = sum(last28) / 28.0
    acwr = round(acute / chronic, 2) if chronic > 0 else None

    mean7 = sum(last7) / 7.0
    var7 = sum((x - mean7) ** 2 for x in last7) / 7.0
    sd7 = math.sqrt(var7)
    monotony = round(mean7 / sd7, 2) if sd7 > 0 else None
    strain = round(sum(last7) * monotony, 0) if monotony else None

    if acwr is None:
        flag = "unknown"
    elif acwr < 0.8:
        flag = "low"
    elif acwr <= 1.3:
        flag = "sweet_spot"
    elif acwr <= 1.5:
        flag = "high"
    else:
        flag = "very_high"

    return {
        "acwr": acwr, "monotony": monotony, "strain": strain, "flag": flag,
        "acute7": round(sum(last7), 0), "chronic28_weekly": round(chronic * 7, 0),
    }


def _in_range(a, start, end):
    try:
        d = dt.datetime.fromisoformat(a["start_date_local"].replace("Z", ""))
    except Exception:
        return False
    return start <= d < end


def compute_weekly_rolling(activities):
    """Last-7-days vs previous-7-days comparison, matching the 'Weekly
    Rolling View' style comparison."""
    now = dt.datetime.now()
    last7_start, prev7_start = now - dt.timedelta(days=7), now - dt.timedelta(days=14)

    def summarize(acts):
        sessions = len(acts)
        hours = sum(min(a.get("moving_time", 0) / 3600.0, LOAD_CAP_HOURS) for a in acts)
        load = sum(daily_load_from_activity(a) for a in acts)
        distance_mi = sum((a.get("distance") or 0) for a in acts) / 1609.344
        hr_acts = [a for a in acts if a.get("average_heartrate")]
        avg_hr = sum(a["average_heartrate"] for a in hr_acts) / len(hr_acts) if hr_acts else None
        run_acts = [a for a in acts if bucket_sport(a.get("sport_type") or a.get("type")) == "Run"
                    and (a.get("distance") or 0) > 0]
        if run_acts:
            total_mi = sum(a["distance"] for a in run_acts) / 1609.344
            total_min = sum(a["moving_time"] for a in run_acts) / 60.0
            avg_pace = total_min / total_mi if total_mi > 0 else None
        else:
            avg_pace = None
        return {
            "sessions": sessions, "hours": round(hours, 1), "load": round(load, 0),
            "distance_mi": round(distance_mi, 1),
            "avg_hr": round(avg_hr) if avg_hr else None,
            "avg_pace_min_per_mi": round(avg_pace, 2) if avg_pace else None,
        }

    current = [a for a in activities if _in_range(a, last7_start, now)]
    previous = [a for a in activities if _in_range(a, prev7_start, last7_start)]
    return {"current": summarize(current), "previous": summarize(previous)}


def compute_activity_log(activities, limit=12):
    rows = []
    for a in sorted(activities, key=lambda x: x.get("start_date_local", ""), reverse=True)[:limit]:
        rows.append({
            "date": (a.get("start_date_local") or "")[:10],
            "name": a.get("name"),
            "sport": bucket_sport(a.get("sport_type") or a.get("type")),
            "duration_min": round((a.get("moving_time") or 0) / 60.0),
            "distance_mi": round((a.get("distance") or 0) / 1609.344, 2),
            "avg_hr": a.get("average_heartrate"),
            "avg_watts": a.get("average_watts"),
        })
    return rows


def compute_pace_trend(activities, sport):
    """Pace over time for Run (min/mi) or Swim (min/100yd)."""
    points = []
    for a in activities:
        if bucket_sport(a.get("sport_type") or a.get("type")) != sport:
            continue
        distance_m = a.get("distance") or 0
        moving_s = a.get("moving_time") or 0
        if distance_m <= 0 or moving_s <= 0:
            continue
        try:
            start = dt.datetime.fromisoformat(a["start_date_local"].replace("Z", ""))
        except Exception:
            continue
        if sport == "Run":
            miles = distance_m / 1609.344
            if miles < 0.5:
                continue
            pace = (moving_s / 60.0) / miles
        else:  # Swim
            yards = distance_m * 1.09361
            if yards < 200:
                continue
            pace = (moving_s / 60.0) / (yards / 100.0)
        points.append({"date": start.date().isoformat(), "value": round(pace, 2)})
    points.sort(key=lambda p: p["date"])
    return points


def compute_aerobic_efficiency(activities, sport):
    """Bike: avg HR vs avg power. Run: avg HR vs pace (min/mi)."""
    points = []
    for a in activities:
        if bucket_sport(a.get("sport_type") or a.get("type")) != sport:
            continue
        hr = a.get("average_heartrate")
        if not hr:
            continue
        if sport == "Bike":
            watts = a.get("average_watts")
            if not watts:
                continue
            points.append({"x": hr, "y": round(watts)})
        elif sport == "Run":
            distance_m = a.get("distance") or 0
            moving_s = a.get("moving_time") or 0
            if distance_m <= 0 or moving_s <= 0:
                continue
            miles = distance_m / 1609.344
            if miles < 0.5:
                continue
            points.append({"x": hr, "y": round((moving_s / 60.0) / miles, 2)})
    return points


def power_zones(ftp):
    bounds = [0, 0.55, 0.76, 0.91, 1.06, 1.21, 1.51]
    names = ["Z1 Active Recovery", "Z2 Endurance", "Z3 Tempo", "Z4 Threshold",
             "Z5 VO2max", "Z6 Anaerobic", "Z7 Neuromuscular"]
    zones = []
    for i, name in enumerate(names):
        lo = round(bounds[i] * ftp)
        hi = round(bounds[i + 1] * ftp) if i + 1 < len(bounds) else None
        zones.append({"name": name, "low": lo, "high": hi})
    return zones


def hr_zones(lthr):
    bounds = [0, 0.81, 0.89, 0.93, 0.99, 1.06]
    names = ["Z1 Recovery", "Z2 Aerobic", "Z3 Tempo", "Z4 Threshold", "Z5 VO2max"]
    zones = []
    for i, name in enumerate(names):
        lo = round(bounds[i] * lthr)
        hi = round(bounds[i + 1] * lthr) if i + 1 < len(bounds) else None
        zones.append({"name": name, "low": lo, "high": hi})
    return zones


# ---------------------------------------------------------------------------
# Coach's note
# ---------------------------------------------------------------------------
def generate_coach_note(garmin_daily, current_fitness, days_out):
    """Rule-based daily readiness call — the same kind of judgment a
    readiness-based coaching app is selling, applied to your own data with
    no subscription and no third party in the loop.

    This is deliberately simple and transparent (thresholds, not a model)
    so you can see exactly why it says what it says and adjust the
    thresholds yourself if they don't match how your body actually
    behaves.
    """
    valid_days = [d for d in garmin_daily if d.get("readiness") is not None]
    if not valid_days:
        return {"flag": "flat", "headline": "No readiness data yet",
                "detail": "Today's Garmin readiness score hasn't synced. Go by feel."}

    today = valid_days[-1]
    readiness = today.get("readiness")
    sleep = today.get("sleepScore")
    hrv_today = today.get("hrvWeekly")

    # 7-day-ago HRV for a trend read, not just a single noisy day
    hrv_week_ago = None
    if len(valid_days) >= 8:
        hrv_week_ago = valid_days[-8].get("hrvWeekly")
    hrv_drop = (hrv_week_ago is not None and hrv_today is not None
                and hrv_today < hrv_week_ago - 3)

    tsb = current_fitness.get("tsb", 0)

    reasons = []
    if readiness is not None:
        reasons.append(f"readiness {readiness}/100")
    if sleep is not None:
        reasons.append(f"sleep score {sleep}")
    if hrv_today is not None:
        reasons.append(f"HRV {hrv_today}ms" + (" (down from last week)" if hrv_drop else ""))
    reasons.append(f"TSB {tsb:+.1f}")
    detail_data = ", ".join(reasons)

    # thresholds — tune these to taste
    red = (readiness is not None and readiness < 35) or tsb < -20 or hrv_drop
    amber = (readiness is not None and readiness < 55) or tsb < -10 or (sleep is not None and sleep < 55)

    taper_zone = days_out is not None and 0 <= days_out <= 10

    if taper_zone:
        flag, headline = "amber", "Taper week — protect freshness, not fitness"
        detail = f"{detail_data}. This close to race day, err toward easy over hard if the two are in tension."
    elif red:
        flag, headline = "red", "Recovery-day territory"
        detail = f"{detail_data}. This is the combination the readiness-based coaching apps are built to catch — take the easy option today rather than the planned hard one."
    elif amber:
        flag, headline = "amber", "Proceed, but don't chase the plan blindly"
        detail = f"{detail_data}. Fine for a moderate/aerobic session; save intensity for a day that reads better."
    else:
        flag, headline = "green", "Green light for a quality session"
        detail = f"{detail_data}. Recovery markers support today's harder work as planned."

    return {"flag": flag, "headline": headline, "detail": detail}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    missing = [n for n, v in [
        ("STRAVA_CLIENT_ID", STRAVA_CLIENT_ID),
        ("STRAVA_CLIENT_SECRET", STRAVA_CLIENT_SECRET),
        ("STRAVA_REFRESH_TOKEN", STRAVA_REFRESH_TOKEN),
    ] if not v]
    if missing:
        log(f"ERROR: missing required env vars: {', '.join(missing)}")
        sys.exit(1)

    log("Fetching Garmin data...")
    garmin_daily, vo2max = fetch_garmin(GARMIN_HISTORY_DAYS)

    log("Fetching Strava activities...")
    activities = fetch_strava_activities(STRAVA_HISTORY_DAYS)

    log("Computing weekly hours by sport...")
    weekly_hours = compute_weekly_hours(activities)
    # keep only the most recent 15 weeks for chart readability
    weekly_hours = weekly_hours[-26:]

    log("Computing CTL/ATL/TSB model and load balance...")
    daily_load = build_daily_load(activities)
    tp_weekly, current_fitness = compute_ctl_atl_tsb(daily_load, history_days=150)
    load_balance = compute_load_balance(daily_load)

    log("Computing weekly rolling comparison, activity log, pace & efficiency...")
    weekly_rolling = compute_weekly_rolling(activities)
    activity_log = compute_activity_log(activities, limit=12)
    pace_run = compute_pace_trend(activities, "Run")[-30:]
    pace_swim = compute_pace_trend(activities, "Swim")[-30:]
    aero_bike = compute_aerobic_efficiency(activities, "Bike")[-60:]
    aero_run = compute_aerobic_efficiency(activities, "Run")[-60:]

    race_date = dt.date.fromisoformat(RACE_DATE)
    days_out = (race_date - dt.date.today()).days

    log("Generating coach's note...")
    coach_note = generate_coach_note(garmin_daily, current_fitness, days_out)

    data = {
        "generatedAt": dt.date.today().isoformat(),
        "race": {
            "name": RACE_NAME,
            "date": RACE_DATE,
            "daysOut": days_out,
            "weeksOut": round(days_out / 7, 1),
        },
        "currentFitness": current_fitness,
        "tpWeekly": tp_weekly,
        "garminDaily": garmin_daily,
        "vo2max": vo2max,
        "weeklyHours": weekly_hours,
        "coachNote": coach_note,
        "loadBalance": load_balance,
        "weeklyRolling": weekly_rolling,
        "activityLog": activity_log,
        "paceRun": pace_run,
        "paceSwim": pace_swim,
        "aeroBike": aero_bike,
        "aeroRun": aero_run,
        "thresholds": THRESHOLDS,
        "powerZones": power_zones(THRESHOLDS["ftp_watts"]),
        "hrZonesBike": hr_zones(THRESHOLDS["lthr_bike"]),
        "hrZonesRun": hr_zones(THRESHOLDS["lthr_run"]),
    }

    log(f"Rendering template {TEMPLATE_PATH} -> {OUTPUT_PATH}")
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()

    html = template.replace("__DATA_JSON__", json.dumps(data))

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    log(f"Wrote {OUTPUT_PATH} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
