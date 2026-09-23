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

GSTT_LAT, GSTT_LON = 51.5045, -0.0865   # Southwark / GSTT
GOSH_LAT, GOSH_LON = 51.5242, -0.1234   # GOSH route centroid
LAT, LON = GSTT_LAT, GSTT_LON
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

def get_forecast(days=2, lat=None, lon=None):
    la = lat if lat is not None else LAT
    lo = lon if lon is not None else LON
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={la}&longitude={lo}"
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

def _slot_line(slot, kp=None):
    t, w, go = slot
    if kp is not None:
        return f"{t} — {w}. KP {kp}."
    return f"{t} — {w}, " + ("all under" if go else "exceeds") + f" the {WIND_LIMIT} m/s limit."

def _hi(slot):
    """peak m/s parsed from a slot's wind string"""
    try:
        return float(slot[1].replace("winds","").replace("m/s","").split("–")[-1].strip())
    except Exception:
        return None

def weather_block(fc, fc_gosh, day_str, kp, label, date_label):
    am = slot_summary(day_window(fc, day_str, 8, 12))
    pm = slot_summary(day_window(fc, day_str, 13, 17))
    lines = [f"🌬️ *{label}* ({date_label})", "*GSTT*"]
    if am: lines.append("AM: " + _slot_line(am))
    if pm: lines.append("PM: " + _slot_line(pm, kp))
    g_am = slot_summary(day_window(fc_gosh, day_str, 8, 12))
    g_pm = slot_summary(day_window(fc_gosh, day_str, 13, 17))
    note = []
    for tag, sslot, gslot in [("AM", am, g_am), ("PM", pm, g_pm)]:
        if not sslot or not gslot:
            continue
        if sslot[2] != gslot[2]:
            note.append(f"{tag} {'GO' if gslot[2] else 'NOGO'} ({gslot[1]})")
            continue
        sh, gh = _hi(sslot), _hi(gslot)
        if sh is not None and gh is not None and abs(sh - gh) >= 0.3:
            note.append(f"{tag} {gslot[1]}")
    if note:
        lines.append("*GOSH* — slightly different — " + "; ".join(note) + ".")
    else:
        lines.append("*GOSH* — same as GSTT.")
    return "\n".join(lines)

def post(webhook, text):
    if not webhook:
        print("no webhook set", file=sys.stderr); return
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        print("posted:", r.status)

import re as _re

def _find_area(notams, kind):
    """kind: 'TRA' (EGTR) for GSTT, 'TDA' (EGD) for GOSH. Return (designator, from, to) active today, or None."""
    pat = r"(EGTR\d+)" if kind == "TRA" else r"(EGD\d+[A-Z]?)"
    for n in notams:
        if not n.get("active_today"):
            continue
        t = n["text"].upper()
        if kind == "TRA" and "TRA" not in t and "RESERVED" not in t:
            continue
        if kind == "TDA" and "TDA" not in t and "DANGER" not in t:
            continue
        m = _re.search(pat, t)
        if not m:
            continue
        # pull FROM/TO times
        fm = _re.search(r"FROM:\s*([0-9]{2} [A-Z]{3} [0-9]{4} [0-9]{2}:[0-9]{2})", t)
        to = _re.search(r"TO:\s*([0-9]{2} [A-Z]{3} [0-9]{4} [0-9]{2}:[0-9]{2})", t)
        sch = _re.search(r"SCHEDULE:\s*([0-9]{4}-[0-9]{4})", t)
        win = sch.group(1) if sch else ((fm.group(1)[-5:] + "-" + to.group(1)[-5:]) if fm and to else "")
        return (m.group(1), win)
    return None

def _point_in_poly(lon, lat, poly):
    """ray casting. poly is list of [lon,lat]."""
    inside = False
    n = len(poly); j = n - 1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside

def notam_summary():
    """Per route: jamming check + TRA/TDA active confirmation (times in Zulu from feed)."""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    def load(name):
        spec = importlib.util.spec_from_file_location(name[:-3], os.path.join(here, name))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
    lines = ["✈️ *NOTAMs*"]
    for label, fn, kind in [("GSTT", "southwark_notam_report.py", "TRA"),
                            ("GOSH", "gosh_notam_report.py", "TDA")]:
        try:
            mod = load(fn)
            notams = mod.fetch_notams()
            active = [n for n in notams if n.get("active_today")]
            jam = [n for n in active if n.get("jamming") or mod.is_jamming_notam(f"{n['id']} {n['text']}")]
            area = _find_area(notams, kind)
            poly = getattr(mod, "TRA_POLYGON", None) or getattr(mod, "CORRIDOR_POLYGON", None)
            if jam:
                lines.append(f"*{label}* — 🔴 NOGO — GPS jamming active.")
            else:
                l = f"*{label}* — 🟢 GO — no jamming."
                if area:
                    l += f" {kind} {area[0]} active" + (f" {area[1]} Zulu." if area[1] else ".")
                else:
                    l += f" No {kind} active today."
                lines.append(l)
                # NOTAMs falling INSIDE our TRA/TDA boundary
                if poly:
                    inside = []
                    for n in notams:
                        if not n.get("active_today"): continue
                        c = n.get("coord")
                        if not c: continue
                        lat_n, lon_n = c[0], c[1]
                        if _point_in_poly(lon_n, lat_n, poly):
                            inside.append(n["id"])
                    if inside:
                        lines.append(f"   ⚠ Inside {kind}: " + ", ".join(inside))
                    else:
                        lines.append(f"   No NOTAMs inside the {kind} boundary.")
        except Exception as e:
            lines.append(f"*{label}* — ⚪ status unavailable ({e}).")
    return "\n".join(lines)

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    wx_hook = os.environ.get("WEATHER_WEBHOOK", "")
    nt_hook = os.environ.get("NOTAM_WEBHOOK", "")
    today = date.today()

    if mode == "weather-today":
        fc = get_forecast(2); fcg = get_forecast(2, GOSH_LAT, GOSH_LON); kp = fetch_kp()
        d = today.strftime("%Y-%m-%d")
        txt = weather_block(fc, fcg, d, kp, "Weather Today", today.strftime("%A %d %B %Y"))
        post(wx_hook, txt)

    elif mode == "weather-tomorrow":
        fc = get_forecast(3); fcg = get_forecast(3, GOSH_LAT, GOSH_LON); kp = fetch_kp()
        tmr = today + timedelta(days=1)
        d = tmr.strftime("%Y-%m-%d")
        txt = weather_block(fc, fcg, d, kp, "Weather Tomorrow's Forecast", tmr.strftime("%A %d %B %Y"))
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
