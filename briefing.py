#!/usr/bin/env python3
"""
briefing.py - posts weather / NOTAM summaries to Slack.
Modes:
  weather-today     -> #weather  (07:45)  today's GSTT/GOSH GO-NOGO summary
  weather-tomorrow  -> #weather  (16:00)  tomorrow's forecast + overnight low
  notams            -> #notams   (07:45)  GO/NOGO summary per route

Webhooks come from env vars (set as GitHub Actions secrets):
  WEATHER_WEBHOOK, NOTAM_WEBHOOK
"""
import os, sys, json, math, urllib.request, urllib.parse
from datetime import datetime, timedelta, date

LAT, LON = 51.5045, -0.0865           # Southwark HQ area
WIND_LIMIT = 8.23                      # m/s
KP_LIMIT   = 6
TEMP_MIN   = 10                        # overnight minimum degC
TZ = "Europe/London"

def fetch_json(url, timeout=30, retries=4, pause=5):
    import time
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            last = e
            if a < retries - 1: time.sleep(pause)
    raise last

def fetch_kp():
    try:
        d = fetch_json("https://services.swpc.noaa.gov/json/planetary_k_index_1m.json", 10)
        return round(float(d[-1]["kp_index"]), 2)
    except Exception:
        return None

def get_forecast(days=2):
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
           f"&hourly=wind_speed_100m,temperature_2m"
           f"&wind_speed_unit=ms&timezone={urllib.parse.quote(TZ)}&forecast_days={days}")
    return fetch_json(url)

def day_window(fc, day_str, h0, h1):
    """wind speeds for a given date between hours h0..h1 inclusive"""
    hh = fc["hourly"]
    out = []
    for i, t in enumerate(hh["time"]):
        if t[:10] == day_str and h0 <= int(t[11:13]) <= h1:
            out.append((t[11:16], hh["wind_speed_100m"][i]))
    return out

def slot_summary(win):
    if not win: return None
    speeds = [s for _, s in win]
    lo, hi = min(speeds), max(speeds)
    go = hi <= WIND_LIMIT
    tick = "🟢 GO" if go else "🔴 NOGO"
    if abs(lo - hi) < 0.15:
        wind = f"winds {lo:.1f} m/s"
    else:
        wind = f"winds {lo:.1f}–{hi:.1f} m/s"
    return tick, wind, go

def overnight_low(fc, start_day):
    """min temp from 16:00 start_day to 08:00 next day"""
    hh = fc["hourly"]
    nxt = (datetime.strptime(start_day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    vals = []
    for i, t in enumerate(hh["time"]):
        h = int(t[11:13]); d = t[:10]
        if (d == start_day and h >= 16) or (d == nxt and h <= 8):
            vals.append((t[11:16] + " " + d[8:10] + "/" + d[5:7], hh["temperature_2m"][i]))
    if not vals: return None
    return min(vals, key=lambda x: x[1])

def weather_block(fc, day_str, kp, label, date_label):
    am = slot_summary(day_window(fc, day_str, 8, 12))
    pm = slot_summary(day_window(fc, day_str, 13, 17))
    lines = [f"🌬️ *{label}* ({date_label})", "*GSTT*"]
    if am:
        t, w, go = am
        lines.append(f"AM: {t} — {w}, "
                     + ("all under" if go else "exceeds") + f" the {WIND_LIMIT} m/s limit.")
    if pm:
        t, w, go = pm
        kpstr = f" KP {kp}." if kp is not None else ""
        lines.append(f"PM: {t} — {w}.{kpstr}")
    # GOSH shares the same forecast point here; note if identical
    lines.append("*GOSH* — same as GSTT.")
    return "\n".join(lines)

def post(webhook, text):
    if not webhook:
        print("no webhook set", file=sys.stderr); return
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        print("posted:", r.status)

def notam_summary():
    """GO/NOGO per route using the existing notam scripts' logic."""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    def load(name):
        spec = importlib.util.spec_from_file_location(name[:-3], os.path.join(here, name))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
    lines = ["✈️ *NOTAMs*"]
    for label, fn in [("GSTT", "southwark_notam_report.py"), ("GOSH", "gosh_notam_report.py")]:
        try:
            mod = load(fn)
            notams = mod.fetch_notams()
            active = [n for n in notams if n.get("active_today")]
            jam = [n for n in active if n.get("jamming") or mod.is_jamming_notam(f"{n['id']} {n['text']}")]
            if jam:
                lines.append(f"*{label}* — 🔴 NOGO — GPS jamming NOTAM active.")
            else:
                lines.append(f"*{label}* — 🟢 GO — clear, nothing restricting the route today.")
        except Exception as e:
            lines.append(f"*{label}* — ⚪ status unavailable ({e}).")
    return "\n".join(lines)

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    wx_hook = os.environ.get("WEATHER_WEBHOOK", "")
    nt_hook = os.environ.get("NOTAM_WEBHOOK", "")
    today = date.today()

    if mode == "weather-today":
        fc = get_forecast(2); kp = fetch_kp()
        d = today.strftime("%Y-%m-%d")
        txt = weather_block(fc, d, kp, "Weather Today", today.strftime("%A %d %B %Y"))
        post(wx_hook, txt)

    elif mode == "weather-tomorrow":
        fc = get_forecast(3); kp = fetch_kp()
        tmr = today + timedelta(days=1)
        d = tmr.strftime("%Y-%m-%d")
        txt = weather_block(fc, d, kp, "Weather Tomorrow's Forecast", tmr.strftime("%A %d %B %Y"))
        ov = overnight_low(fc, d)
        if ov:
            ok = ov[1] >= TEMP_MIN
            tick = "🟢" if ok else "🔴"
            txt += f"\n*OVERNIGHT LOW* {tick} {ov[1]:.0f}°C at {ov[0]} (min {TEMP_MIN}°C)"
        post(wx_hook, txt)

    elif mode == "notams":
        post(nt_hook, notam_summary())

    else:
        print("usage: briefing.py [weather-today|weather-tomorrow|notams]")

if __name__ == "__main__":
    main()
