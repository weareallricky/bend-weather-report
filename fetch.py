#!/usr/bin/env python3
"""Fetch multi-model point forecasts from Open-Meteo and write data/latest.json.

Standard library only: nothing to install, no third-party code runs.
Each model is fetched separately so one bad model name or outage does not
take down the rest; failures are recorded in the output under "errors".
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.open-meteo.com/v1/forecast"

LOCATIONS = {
    "bend": {"label": "Bend, OR", "latitude": 44.0582, "longitude": -121.3153},
    # Mid-mountain; elevation is given in meters so temperatures are adjusted
    # to ~7,600 ft rather than the model's coarse terrain height.
    "bachelor": {
        "label": "Mt. Bachelor (~7,600 ft)",
        "latitude": 43.979,
        "longitude": -121.688,
        "elevation": 2316,
    },
}

# label -> list of Open-Meteo model ids to try in order (first that works wins)
MODELS = {
    "HRRR": ["gfs_hrrr", "ncep_hrrr_conus"],
    "NAM": ["ncep_nam_conus"],
    "NBM": ["ncep_nbm_conus"],
    "GFS": ["gfs_global", "gfs_seamless"],  # gfs_seamless blends in HRRR, so prefer pure GFS
    "ECMWF": ["ecmwf_ifs025", "ecmwf_ifs"],
}

DAILY = [
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "snowfall_sum",
    "precipitation_probability_max",
    "wind_speed_10m_max",
    "wind_gusts_10m_max",
]

HOURLY = [
    "temperature_2m",
    "precipitation",
    "snowfall",
    "wind_speed_10m",
    "wind_gusts_10m",
    "cloud_cover",
    "freezing_level_height",
]


def fetch(loc, model_id):
    params = {
        "latitude": loc["latitude"],
        "longitude": loc["longitude"],
        "models": model_id,
        "daily": ",".join(DAILY),
        "hourly": ",".join(HOURLY),
        "forecast_days": 8,
        "forecast_hours": 48,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "America/Los_Angeles",
    }
    if "elevation" in loc:
        params["elevation"] = loc["elevation"]
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "bend-weather-data"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$")
UNIT_RE = re.compile(r"^[A-Za-z0-9°%/ .\-]{0,16}$")


def clean_block(block, allowed):
    """Keep only expected keys, and only numbers (or null) as values.

    The output of this script is read by an AI agent, so nothing free-form
    from the API is passed through: no unexpected keys, no text values.
    """
    out = {}
    for key, series in (block or {}).items():
        if not isinstance(series, list):
            continue
        if key == "time":
            out[key] = [t for t in series if isinstance(t, str) and TIME_RE.match(t)]
        elif key in allowed:
            out[key] = [
                v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
                for v in series
            ]
    return out


def clean_units(units, allowed):
    return {
        k: v
        for k, v in (units or {}).items()
        if k in allowed and isinstance(v, str) and UNIT_RE.match(v)
    }


def has_data(payload):
    """True if at least one daily or hourly series has a non-null value."""
    for block in ("daily", "hourly"):
        for key, series in (payload.get(block) or {}).items():
            if key != "time" and any(v is not None for v in series):
                return True
    return False


def fetch_model(loc, ids):
    last_error = "no model ids configured"
    for model_id in ids:
        for attempt in range(2):
            try:
                payload = fetch(loc, model_id)
                if payload.get("error"):
                    # Full reason goes to the run log only, not the data file.
                    print(f"  {model_id}: API error: {payload.get('reason')}")
                    last_error = f"{model_id}: API returned an error"
                    break
                payload = {
                    "elevation": payload.get("elevation"),
                    "daily_units": clean_units(payload.get("daily_units"), DAILY),
                    "hourly_units": clean_units(payload.get("hourly_units"), HOURLY),
                    "daily": clean_block(payload.get("daily"), DAILY),
                    "hourly": clean_block(payload.get("hourly"), HOURLY),
                }
                if not has_data(payload):
                    last_error = f"{model_id}: returned only empty values"
                    break
                elev = payload["elevation"]
                return {
                    "model_id": model_id,
                    "grid_elevation_m": elev if isinstance(elev, (int, float)) else None,
                    "daily_units": payload["daily_units"],
                    "hourly_units": payload["hourly_units"],
                    "daily": payload["daily"],
                    "hourly": payload["hourly"],
                }, None
            except urllib.error.HTTPError as exc:
                print(f"  {model_id}: HTTP {exc.code}: {exc.read()[:300]!r}")
                last_error = f"{model_id}: HTTP {exc.code}"
                break  # a 4xx will not improve on retry; try the next id
            except Exception as exc:  # network hiccup: retry once
                print(f"  {model_id}: {type(exc).__name__}: {exc}")
                last_error = f"{model_id}: {type(exc).__name__}"
                time.sleep(5)
    return None, last_error


def main():
    out = {
        "fetched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Open-Meteo (https://open-meteo.com), CC BY 4.0",
        "units": "F, mph, inches; freezing level in feet; times America/Los_Angeles",
        "locations": {},
        "errors": [],
    }
    ok = 0
    for key, loc in LOCATIONS.items():
        entry = {k: v for k, v in loc.items()}
        entry["models"] = {}
        for label, ids in MODELS.items():
            data, err = fetch_model(loc, ids)
            if data:
                entry["models"][label] = data
                ok += 1
            else:
                out["errors"].append(f"{key}/{label}: {err}")
        out["locations"][key] = entry

    os.makedirs("data", exist_ok=True)
    with open("data/latest.json", "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
        fh.write("\n")

    print(f"models fetched: {ok}; errors: {len(out['errors'])}")
    for err in out["errors"]:
        print("  " + err)
    if ok == 0:
        sys.exit("no model data fetched")


if __name__ == "__main__":
    main()
