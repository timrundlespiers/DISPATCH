import os
#!/usr/bin/env python3
"""
Writes qnh.js next to the DISPATCH BOARD page:
EGLC QNH + MATTERNET OPS WEATHER block. Runs on cron (15-min).
Slack wind report is a separate script and is not affected by this file.
"""
import json, os, re, sys, urllib.request
from datetime import datetime

LAT, LON = 51.5036, -0.0881
THRESHOLD_MS   = 8.23     # wind limit (m/s)
THRESHOLD_KP   = 6        # geomagnetic limit
TEMP_MIN       = -10      # operating temp floor (C)
TEMP_MAX       = 40       # operating temp ceiling (C)
PRECIP_MAX     = 12.0     # moderate rain ceiling (mm/h); above => NOGO
VIS_MIN_M      = 25       # minimum visibility (metres)
REPORT_START, REPORT_END = 8, 17
OUT = os.environ.get("OUT_DIR", "/Users/tim.rundle-spiers/Library/CloudStorage/GoogleDrive-tim.rundle-spiers@matternet.com/My Drive/DISPATCH BOARD") + "/qnh.js"

GO   = "\u2705"
NOGO = "\u274c"


def compass(deg):
    dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE',
            'S','SSW','SW','WSW','W','WNW','NW','NNW']
    return dirs[round(deg / 22.5) % 16]


def fetch_json(url, timeout=30, retries=4, pause=5):
    import time
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(pause)
    raise last


def fetch_metar_raw():
    try:
        data = fetch_json(
            "https://aviationweather.gov/api/data/metar?ids=EGLC&format=json", 15)
        if data and data[0].get("rawOb"):
            return data[0]["rawOb"]
    except Exception as e:
        print(f"AWC warning: {e}", file=sys.stderr)
    raw = urllib.request.urlopen(
        "https://tgftp.nws.noaa.gov/data/observations/metar/stations/EGLC.TXT",
        timeout=15).read().decode()
    return raw.strip().splitlines()[-1]


def parse_metar(raw):
    m = {"raw": raw, "obs_time": raw[5:11] if len(raw) > 10 else ""}
    w = re.search(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\b", raw)
    if w:
        m["wind_dir"] = None if w.group(1) == "VRB" else int(w.group(1))
        m["wind_ms"] = round(int(w.group(2)) * 0.51444, 1)
        m["gust_ms"] = round(int(w.group(3)) * 0.51444, 1) if w.group(3) else None
    t = re.search(r"\s(M?\d{2})/(M?\d{2})\b", raw)
    if t:
        m["temp"] = int(t.group(1).replace("M", "-"))
    q = re.search(r"\bQ(\d{4})\b", raw)
    if q:
        m["qnh"] = int(q.group(1))
    # METAR visibility: 4-digit metres group, or 9999 = 10km+
    v = re.search(r"\s(\d{4})\s", raw)
    if "CAVOK" in raw:
        m["vis_m"] = 10000
    elif v:
        m["vis_m"] = int(v.group(1))
    return m


def fetch_kp():
    try:
        data = fetch_json(
            "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json", 10)
        today = datetime.now().strftime("%Y-%m-%d")
        vals = [float(e["estimated_kp"]) for e in data
                if e.get("time_tag", "").startswith(today)
                and e.get("estimated_kp") is not None]
        if vals:
            return round(max(vals), 2)
        return round(float(data[-1]["estimated_kp"]), 2) if data else None
    except Exception as e:
        print(f"KP warning: {e}", file=sys.stderr)
        return None


def build_ops(data, kp_val, metar, extra):
    times  = data["hourly"]["time"]
    speeds = data["hourly"]["wind_speed_100m"]
    dirs   = data["hourly"]["wind_direction_100m"]
    gusts  = data["hourly"]["wind_gusts_10m"]

    kp_nogo   = kp_val is not None and kp_val >= THRESHOLD_KP
    kp_str    = f"{kp_val}" if kp_val is not None else "N/A"
    kp_status = f"{NOGO} NOGO" if kp_nogo else (f"{GO} OK" if kp_val is not None else "\u2014")

    _today = times[0][:10]
    window = [(t[11:16], s, d, g)
              for t, s, d, g in zip(times, speeds, dirs, gusts)
              if t[:10] == _today and REPORT_START <= int(t[11:13]) <= REPORT_END]

    spds = [w[1] for w in window]
    avg, peak = sum(spds)/len(spds), max(spds)
    peak_time = window[spds.index(peak)][0]
    # current wind = the hour matching now (falls back to latest window hour)
    _now_h = datetime.now().strftime("%H:00")
    _cur = next((w for w in window if w[0] == _now_h), window[-1] if window else None)
    if _cur:
        cur_wind_s = _cur[1]
        _tick = GO if cur_wind_s <= THRESHOLD_MS else NOGO
        cur_wind_field = f"{cur_wind_s:.1f} m/s  {_tick}"
    else:
        cur_wind_field = "\u2014"
    avg_dir   = round(sum(w[2] for w in window) / len(window))

    nogo_hours   = [(t, s) for t, s, d, g in window if s > THRESHOLD_MS]
    nogo_pct     = round(len(nogo_hours) / len(window) * 100)
    go_pct       = 100 - nogo_pct
    if kp_nogo:
        overall = f"\U0001F534 {go_pct}% GO (KP NOGO)"
    elif nogo_hours:
        overall = f"\U0001F7E1 {go_pct}% GO"
    else:
        overall = "\U0001F7E2 100% GO"

    # ---- METAR wind field ----
    if metar and "wind_ms" in metar:
        gust_str = f"  gust {metar['gust_ms']} m/s" if metar.get("gust_ms") else ""
        qnh_str  = f"  QNH {metar['qnh']}hPa" if metar.get("qnh") else ""
        tick = GO if metar["wind_ms"] <= THRESHOLD_MS else NOGO
        d = metar.get("wind_dir")
        dir_str = f"{compass(d)} {d}\u00b0" if d is not None else "VRB"
        wind_field = f"{metar['wind_ms']} m/s  {dir_str}{gust_str}{qnh_str}  {tick}  {metar['obs_time']}Z"
    else:
        wind_field = "Unavailable"

    ovl=extra.get("ov_low"); ovlt=extra.get("ov_low_t")
    if ovl is None:
        ov_low_field="\u2014"
    else:
        tick=GO if ovl>=10 else NOGO
        ov_low_field=f"{ovl:.0f}\u00b0C at {ovlt}  {tick}"
    # ---- Temperature fields ----
    cur_t = metar.get("temp") if metar else None
    if cur_t is not None:
        t_ok = TEMP_MIN <= cur_t <= TEMP_MAX
        cur_temp_field = f"{cur_t}\u00b0C  {GO if t_ok else NOGO}"
    else:
        cur_temp_field = "\u2014"

    max_t = extra.get("temp_max")
    if max_t is not None:
        mt_ok = max_t <= TEMP_MAX
        max_temp_field = f"{max_t:.0f}\u00b0C  {GO if mt_ok else NOGO}"
    else:
        max_temp_field = "\u2014"

    # ---- Precipitation ----
    precip = extra.get("precip_max")
    if precip is not None:
        p_ok = precip <= PRECIP_MAX
        precip_field = f"{precip:.1f} mm/h  {GO if p_ok else NOGO}"
    else:
        precip_field = "\u2014"

    # ---- Visibility ----
    vis = metar.get("vis_m") if metar else None
    if vis is None:
        vis = extra.get("vis_m")
    if vis is not None:
        v_ok = vis >= VIS_MIN_M
        vis_txt = "10 km+" if vis >= 9999 else (f"{vis} m" if vis < 5000 else f"{vis/1000:.1f} km")
        vis_field = f"{vis_txt}  {GO if v_ok else NOGO}"
    else:
        vis_field = "\u2014"

    # ---- Hourly wind table (kept) ----
    header = "TIME   SPEED   GUST    DIR    CMP   STATUS"
    rows = header + "\n" + "-" * len(header) + "\n"
    for t, s, d, g in window:
        ok = s <= THRESHOLD_MS
        status = f"{GO} GO  " if (ok and not kp_nogo) else f"{NOGO} NOGO"
        rows += f"{t}   {s:4.1f}    {g:4.1f}   {d:3.0f}\u00b0   {compass(d):<4}  {status}\n"

    return {
        "date": datetime.now().strftime("%A %d %B %Y"),
        "fields": [
            ["Overall", overall],
            ["KP Index", f"{kp_str}  {kp_status}"],
            ["Current wind", cur_wind_field],
            ["Direction", f"{compass(avg_dir)} ({avg_dir}\u00b0)"],
            ["Current temp", cur_temp_field],
            ["Max temp", max_temp_field],
            ["Overnight low", ov_low_field],
            ["Precipitation", precip_field],
            ["Visibility", vis_field],
            ["EGLC METAR \u2014 actual, surface level", wind_field],
        ],
        "rows": rows.rstrip(),
    }


if __name__ == "__main__":
    raw = fetch_metar_raw()
    metar = parse_metar(raw)
    wind = fetch_json(
        f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
        f"&hourly=wind_speed_100m,wind_direction_100m,wind_gusts_10m,precipitation,visibility,temperature_2m"
        f"&daily=temperature_2m_max"
        f"&wind_speed_unit=ms&timezone=Europe%2FLondon&forecast_days=2")

    # daytime-window extras
    hh = wind["hourly"]
    win_idx = [i for i, t in enumerate(hh["time"]) if REPORT_START <= int(t[11:13]) <= REPORT_END]
    precip_vals = [hh["precipitation"][i] for i in win_idx if hh.get("precipitation")]
    vis_vals    = [hh["visibility"][i] for i in win_idx if hh.get("visibility")]
    ov=[]
    for i,t in enumerate(hh["time"]):
        h=int(t[11:13]); d=t[:10]
        if (d==hh["time"][0][:10] and h>=16) or (d>hh["time"][0][:10] and h<=8):
            temp=hh.get("temperature_2m",[None]*len(hh["time"]))[i]
            if temp is not None: ov.append((t[11:16],temp))
    ov_low=min(ov,key=lambda x:x[1]) if ov else None
    extra = {
        "ov_low": ov_low[1] if ov_low else None,
        "ov_low_t": ov_low[0] if ov_low else None,
        "temp_max": (wind.get("daily", {}).get("temperature_2m_max") or [None])[0],
        "precip_max": max(precip_vals) if precip_vals else None,
        "vis_m": min(vis_vals) if vis_vals else None,
    }

    kp = fetch_kp()
    payload = {
        "q": metar.get("qnh"),
        "raw": raw,
        "t": datetime.now().strftime("%H:%M"),
        "ops": build_ops(wind, kp, metar, extra),
    }
    with open(OUT, "w") as f:
        f.write("window.LOCAL_QNH = " + json.dumps(payload) + ";")
    print(raw, "->", OUT)
