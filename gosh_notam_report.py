#!/usr/bin/env python3
"""
GOSH NOTAM Report — weekday 7:45am Slack poster
Centre: GOSH route centroid (51.5242N, 0.1234W)
Route: 51.521305,-0.121177 → 51.523136,-0.121241 → 51.528101,-0.127921
NOTAM search radius: 2.0km
Posts NOTAM list + static map to #notams Slack channel
Retry logic: 08:00 run skips silently if 07:45 already succeeded
"""

import urllib.request
import urllib.parse
import json
import sys
import os
import re
import math
from html.parser import HTMLParser
from datetime import datetime, date, timedelta

# ── CONFIG ───────────────────────────────────────────────────────────────────
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

GEOAPIFY_KEY = "85af2442ac73422ab11407c17d344789"

# Centre: GOSH route centroid
LAT_CENTRE = 51.524181
LON_CENTRE = -0.123446
RADIUS_KM  = 2.0
JAMMING_RADIUS_KM = 50.0

# GOSH route waypoints for map corridor
ROUTE_POINTS = [
    (-0.121177, 51.521305),
    (-0.121241, 51.523136),
    (-0.127921, 51.528101),
]

# 50m corridor polygon around GOSH flight route with rounded end caps
# 50m corridor polygon around GOSH flight route with straight extended ends
CORRIDOR_POLYGON = [
    [-0.12188, 51.52085],
    [-0.12190, 51.52129],
    [-0.12192, 51.52298],
    [-0.12848, 51.52781],
    [-0.12894, 51.52816],
    [-0.12784, 51.52874],
    [-0.12737, 51.52839],
    [-0.12056, 51.52329],
    [-0.12045, 51.52132],
    [-0.12044, 51.52087],
    [-0.12188, 51.52085],
]

NATS_FEEDS = [
    "http://pibs.nats.co.uk/operational/pibs/pib3.shtml",    # London FIR IFR/VFR
    "http://pibs.nats.co.uk/operational/pibs/pib51n.shtml",  # Aerodromes 51N-52N
    "http://pibs.nats.co.uk/operational/pibs/pib4.shtml",    # En-route — includes GPS jamming
    "http://pibs.nats.co.uk/operational/pibs/pib5.shtml",    # Danger/Restricted areas
    "http://pibs.nats.co.uk/operational/pibs/pib6.shtml",    # Navigation warnings
]

STATE_FILE  = os.path.expanduser("~/scripts/gosh_notam_report_sent.txt")
NOTAM_ID_RE = re.compile(r'^([A-Z]\d{4}/\d{2,4})$')

# GPS-specific Q-codes kept for reference only — the banner decision now
# rests entirely on JAM_KEYWORDS (see is_jamming_notam). QNVXX/QNAVW were
# removed: they cover generic navaid warnings (VOR/DME cranes, maintenance)
# and caused false jamming banners (e.g. A2734/26, 17 Jul 2026).
JAM_QCODES   = ['QGWXX', 'QGPXX', 'QGPAS', 'QGPWS']
JAM_KEYWORDS = ['JAMMING', 'GNSS (GPS)', 'GPS/GNSS', 'GPS UNRELIABLE',
                'SIGNAL UNRELIABLE', 'GPS OUTAGE', 'GNSS SIGNAL',
                'GPS JAMMING', 'GNSS JAMMING', 'GNSS INTERFERENCE',
                'GPS INTERFERENCE', 'GNSS UNRELIABLE']
# ─────────────────────────────────────────────────────────────────────────────


def already_sent_today():
    if not os.path.exists(STATE_FILE):
        return False
    with open(STATE_FILE) as f:
        return f.read().strip() == str(date.today())


def mark_sent_today():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        f.write(str(date.today()))


def fetch_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": "NOTAMReporter/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


class NotamHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks = []
        self._buf = []

    def handle_starttag(self, tag, attrs):
        self._flush()

    def handle_endtag(self, tag):
        self._flush()

    def handle_data(self, data):
        s = data.strip()
        if s:
            self._buf.append(s)

    def _flush(self):
        t = " ".join(self._buf).strip()
        if t:
            self.blocks.append(t)
        self._buf = []

    def get_blocks(self):
        self._flush()
        return self.blocks


def group_notams(blocks):
    """
    NATS PIB HTML structure per NOTAM:
        [Q-line + body text]  [ID]  [FROM:] [date] [TO:] [date]
    Body comes BEFORE the ID; FROM/TO comes AFTER the ID.
    """
    notams = []
    pending_parts = []
    i = 0
    n = len(blocks)

    while i < n:
        block = blocks[i]
        stripped = block.strip()

        if NOTAM_ID_RE.match(stripped):
            body_text = " ".join(pending_parts).strip()
            pending_parts = []

            # Capture FROM/TO blocks immediately following the ID
            date_parts = []
            j = i + 1
            while j < n:
                nxt = blocks[j].strip()
                if nxt in ("Q)", "A)") or NOTAM_ID_RE.match(nxt):
                    break
                date_parts.append(blocks[j])
                j += 1
            i = j - 1

            full_text = (" ".join(date_parts).strip() + " " + body_text).strip() if date_parts else body_text
            notams.append({"id": stripped, "text": full_text.strip()})
        else:
            pending_parts.append(block)

        i += 1

    if notams and pending_parts:
        notams[-1]["text"] = (notams[-1]["text"] + " " + " ".join(pending_parts)).strip()

    return [n for n in notams if len(n["text"]) > 20]


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def parse_coords(text):
    results = []
    for m in re.finditer(r'(\d{6})N\s*(\d{7})W', text):
        lat = int(m.group(1)[:2]) + int(m.group(1)[2:4]) / 60.0 + int(m.group(1)[4:]) / 3600.0
        lon = -(int(m.group(2)[:3]) + int(m.group(2)[3:5]) / 60.0 + int(m.group(2)[5:]) / 3600.0)
        results.append((lat, lon))
    if not results:
        for m in re.finditer(r'(\d{4})N(\d{5})W', text):
            lat = int(m.group(1)[:2]) + int(m.group(1)[2:]) / 60.0
            lon = -(int(m.group(2)[:3]) + int(m.group(2)[3:]) / 60.0)
            results.append((lat, lon))
    return results


def within_radius(text, radius_km=None):
    if radius_km is None:
        radius_km = RADIUS_KM
    best_dist = None
    best_coord = None
    for lat, lon in parse_coords(text):
        dist = haversine_km(LAT_CENTRE, LON_CENTRE, lat, lon)
        if dist <= radius_km:
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_coord = (lat, lon)
    return best_coord, best_dist


def is_jamming_notam(text):
    # Keyword match in the NOTAM text is REQUIRED. A Q-code alone is not
    # sufficient — Q-codes classify the navaid, not the hazard, and
    # matching on them alone produced false jamming banners (A2734/26).
    t = text.upper()
    return any(kw in t for kw in JAM_KEYWORDS)


MONTHS = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
          'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}


def parse_notam_date(s):
    m = re.search(r'(\d{1,2})\s+([A-Z]{3})\s+(\d{4})\s+(\d{2}):(\d{2})', s)
    if m:
        try:
            return datetime(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)),
                            int(m.group(4)), int(m.group(5)))
        except Exception:
            pass
    return None


def is_active_today(text):
    now         = datetime.utcnow()
    today_start = now.replace(hour=0,  minute=0,  second=0,  microsecond=0)
    today_end   = now.replace(hour=23, minute=59, second=59, microsecond=0)

    from_m = re.search(r'FROM:\s+(.{10,20}?)\s+TO:', text)
    if not from_m:
        return False
    b_dt = parse_notam_date(from_m.group(1))
    if not b_dt:
        return False

    if re.search(r'TO:\s*PERM', text, re.IGNORECASE):
        c_dt = today_end
    else:
        to_m = re.search(r'TO:\s+(.{10,20}?)(?:\s+SCHEDULE|\s+\+|\s+Q\)|\s*$)', text)
        c_dt = parse_notam_date(to_m.group(1)) if to_m else None

    if not c_dt:
        return False
    return b_dt <= today_end and c_dt >= today_start


def is_active_tomorrow(text):
    now       = datetime.utcnow()
    tom_start = (now + timedelta(days=1)).replace(hour=0,  minute=0,  second=0,  microsecond=0)
    tom_end   = (now + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)

    from_m = re.search(r'FROM:\s+(.{10,20}?)\s+TO:', text)
    if not from_m:
        return False
    b_dt = parse_notam_date(from_m.group(1))
    if not b_dt:
        return False

    if re.search(r'TO:\s*PERM', text, re.IGNORECASE):
        c_dt = tom_end
    else:
        to_m = re.search(r'TO:\s+(.{10,20}?)(?:\s+SCHEDULE|\s+\+|\s+Q\)|\s*$)', text)
        c_dt = parse_notam_date(to_m.group(1)) if to_m else None

    if not c_dt:
        return False

    active_tomorrow = b_dt <= tom_end and c_dt >= tom_start
    active_today    = b_dt <= today_end and c_dt >= now.replace(hour=0, minute=0, second=0, microsecond=0)
    return active_tomorrow and not active_today


def fetch_notams():
    notams   = []
    seen_ids = set()

    for url in NATS_FEEDS:
        try:
            print(f"  Fetching {url} ...")
            html    = fetch_url(url)
            parser  = NotamHTMLParser()
            parser.feed(html)
            grouped = group_notams(parser.get_blocks())
            print(f"    Parsed {len(grouped)} NOTAMs")

            for n in grouped:
                if n["id"] in seen_ids:
                    continue
                full_text = f"{n['id']} {n['text']}"

                # Jamming — 50km radius
                if is_jamming_notam(full_text) and (is_active_today(n["text"]) or is_active_tomorrow(n["text"])):
                    jam_coord, jam_dist = within_radius(full_text, radius_km=JAMMING_RADIUS_KM)
                    if jam_coord is None:
                        continue
                    seen_ids.add(n["id"])
                    notams.append({
                        "id":              n["id"],
                        "text":            n["text"][:500],
                        "dist":            round(jam_dist, 1),
                        "coord":           jam_coord,
                        "active_today":    is_active_today(n["text"]),
                        "active_tomorrow": is_active_tomorrow(n["text"]),
                        "jamming":         True,
                    })
                    continue

                coord, dist = within_radius(full_text)
                if coord is None:
                    continue
                seen_ids.add(n["id"])
                notams.append({
                    "id":              n["id"],
                    "text":            n["text"][:500],
                    "dist":            round(dist, 2),
                    "coord":           coord,
                    "active_today":    is_active_today(n["text"]),
                    "active_tomorrow": is_active_tomorrow(n["text"]),
                    "jamming":         False,
                })

        except Exception as e:
            print(f"  Warning: {url} failed — {e}", file=sys.stderr)

    notams.sort(key=lambda x: (0 if x.get("jamming") else 1, x["dist"] or 99))
    print(f"  Total within radius: {len(notams)}")
    return notams


def classify_notam(text):
    t = text.upper()
    if any(k in t for k in ["RESTRICTED", "PROHIBITED", "DANGER", "TFR", "ERF", "TDA"]):
        return "🔴", "RESTRICTED"
    elif any(k in t for k in ["DRONE", "UAS", "UAV", "RPAS", "TRA", "BVLOS"]):
        return "🟠", "UAS/DRONE"
    elif any(k in t for k in ["CRANE", "OBSTACLE", "OBST"]):
        return "🟡", "OBSTACLE"
    elif any(k in t for k in ["ILS", "VOR", "NDB", "DME", "NAVAID", "GPS"]):
        return "🔵", "NAV AID"
    elif any(k in t for k in ["RWY", "RUNWAY", "TWY", "TAXIWAY", "APRON"]):
        return "🟣", "AERODROME"
    elif any(k in t for k in ["AIRSPACE", "CTR", "TMA", "ATZ", "CTA", "FREQ"]):
        return "⚪", "AIRSPACE"
    else:
        return "⚪", "GENERAL"


def sanitise_for_slack(text):
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def marker_color(notam):
    t = notam["text"].upper()
    if any(k in t for k in ["TDA", "RESTRICTED", "PROHIBITED", "DANGER"]):
        return "red"
    elif any(k in t for k in ["TRA", "UAS", "DRONE", "BVLOS", "RPAS"]):
        return "orange"
    else:
        return "yellow"


def build_map_url(notams):
    import json as _json

    SEP = "%7C"

    # Single Feature polygon — corridor only, no LineString
    # Geoapify enforces 1024 char limit on encoded GeoJSON
    geojson = {
        "type": "Feature",
        "properties": {
            "linecolor": "#dc2626", "linewidth": 2, "lineopacity": 0.9,
            "fillcolor": "#dc2626", "fillopacity": 0.25
        },
        "geometry": {"type": "Polygon", "coordinates": [CORRIDOR_POLYGON]}
    }
    geojson_enc = urllib.parse.quote(_json.dumps(geojson, separators=(",", ":")), safe="")

    # Markers
    color_map = {"red": "#dc2626", "orange": "#f97316", "yellow": "#eab308"}
    markers   = [f"lonlat:{LON_CENTRE},{LAT_CENTRE};color:#6366f1;size:medium;icon:star"]
    legend    = []

    for i, n in enumerate(notams[:14], start=1):
        if not n.get("coord"):
            legend.append(n["id"])
            continue
        lat, lon = n["coord"]
        color = color_map.get(marker_color(n), "#888888")
        markers.append(f"lonlat:{lon:.5f},{lat:.5f};color:{color};size:medium;text:{i}")
        legend.append(n["id"])

    markers_enc = SEP.join(urllib.parse.quote(m, safe=";:,.-") for m in markers)

    url = (
        f"https://maps.geoapify.com/v1/staticmap"
        f"?style=osm-bright&width=800&height=400"
        f"&center=lonlat:{LON_CENTRE},{LAT_CENTRE}&zoom=13"
        f"&geojson={geojson_enc}"
        f"&marker={markers_enc}"
        f"&apiKey={GEOAPIFY_KEY}"
    )
    return url, legend


def build_slack_message(notams):
    today = datetime.now().strftime("%A %d %B %Y")
    area  = "GOSH Route  |  2.0km radius"

    if not notams:
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": f"GOSH NOTAM Report - {today}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Area:* {area}"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*No active NOTAMs within 2km.* Airspace is clear."}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Source: NATS AIS UK PIB  |  Mon-Fri 07:45"}]}
        ]
        return {"text": f"GOSH NOTAM Report - {today} | No active NOTAMs", "blocks": blocks}

    count    = len(notams)
    cap_note = f" _(showing 10 of {count})_" if count > 10 else ""

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"GOSH NOTAM Report - {today}"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Area*\n{area}"},
                {"type": "mrkdwn", "text": f"*Active NOTAMs*\n{count}{cap_note}"}
            ]
        },
        {"type": "divider"}
    ]

    # GPS jamming warnings
    jamming_today    = [n for n in notams if n.get("jamming") and n.get("active_today")]
    jamming_tomorrow = [n for n in notams if n.get("jamming") and n.get("active_tomorrow")]
    if jamming_today:
        jam_ids = "  ".join(n["id"] for n in jamming_today)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": (
                "🚨 *OPERATIONAL DISRUPTION — GPS/GNSS JAMMING ACTIVE TODAY*\n"
                "GNSS signals may be unreliable. UAS operations affected.\n"
                f"_{jam_ids}_"
            )}
        })
        blocks.append({"type": "divider"})
    if jamming_tomorrow:
        jam_ids = "  ".join(n["id"] for n in jamming_tomorrow)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": (
                "⚠️ *GPS/GNSS JAMMING NOTAM — ACTIVE TOMORROW*\n"
                "Plan accordingly — GNSS may be unreliable.\n"
                f"_{jam_ids}_"
            )}
        })
        blocks.append({"type": "divider"})

    # Map
    map_url, legend = build_map_url(notams)
    print(f"  Map URL: {map_url[:80]}...")
    print(f"  Legend: {legend}")
    pass  # DROPPED IMAGE:     blocks.append({"type": "image", "image_url": map_url, "alt_text": "GOSH NOTAM area map"})
    if legend:
        key = "  ".join(f"{i+1}={nid.split('/')[0]}" for i, nid in enumerate(legend))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"_{key}_"}})
    blocks.append({"type": "divider"})

    # NOTAM list
    for i, n in enumerate(notams[:10], start=1):
        emoji, label = classify_notam(n["text"])
        dist_str     = f"  _{n['dist']}km_" if n.get("dist") else "  _wide area_"
        today_tag    = "  🔔 *TODAY*" if n.get("active_today") else ""
        jam_tag      = "  🚨 *JAMMING*" if n.get("jamming") else ""
        detail       = sanitise_for_slack(n["text"][:300] + ("..." if len(n["text"]) > 300 else ""))
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{i}.* {emoji} *{n['id']}*  `{label}`{dist_str}{today_tag}{jam_tag}\n{detail}"}
        })
        blocks.append({"type": "divider"})

    # Tomorrow section
    tomorrow_notams = [n for n in notams if n.get("active_tomorrow")]
    if tomorrow_notams:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*NEW TOMORROW*"}})
        for n in tomorrow_notams:
            emoji, label = classify_notam(n["text"])
            dist_str     = f"  _{n['dist']}km_" if n.get("dist") else "  _wide area_"
            detail       = sanitise_for_slack(n["text"][:300] + ("..." if len(n["text"]) > 300 else ""))
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"📅 {emoji} *{n['id']}*  `{label}`{dist_str}\n{detail}"}
            })
            blocks.append({"type": "divider"})

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "🔔 TODAY = active today  |  📅 NEW TOMORROW = starts tomorrow  |  Red corridor = GOSH flight route (100m)  |  Source: NATS AIS  |  Mon-Fri 07:45"}]
    })

    tomorrow_count = len(tomorrow_notams)
    summary = f"GOSH NOTAM Report - {today} | {count} active NOTAM{'s' if count != 1 else ''}"
    if tomorrow_count:
        summary += f" | {tomorrow_count} new tomorrow"

    return {"text": summary, "blocks": blocks}


def post_to_slack(payload):
    data = json.dumps(payload).encode("utf-8")
    # Write debug file
    debug_path = os.path.expanduser("~/scripts/gosh_notam_payload_debug.json")
    with open(debug_path, "w", encoding="utf-8") as dbg:
        json.dump(payload, dbg, indent=2, ensure_ascii=False)

    # Validate blocks
    for i, block in enumerate(payload.get("blocks", [])):
        btype = block.get("type", "")
        if btype == "section":
            txt = block.get("text", {}).get("text", "")
            if len(txt) > 3000:
                print(f"  WARNING block {i}: section text too long ({len(txt)} chars)")
            for ch in ["&", "<", ">"]:
                if ch in txt:
                    print(f"  WARNING block {i}: raw '{ch}' in section text")
        elif btype == "image":
            url = block.get("image_url", "")
            if "|" in url:
                print(f"  WARNING block {i}: raw pipe in image URL")

    print(f"  Payload: {len(data)} bytes, {len(payload.get('blocks', []))} blocks")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            response = r.read().decode()
        if response != "ok":
            raise RuntimeError(f"Slack returned: {response}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"Slack HTTP {e.code}: {body}")


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] GOSH NOTAM report starting...")

    if already_sent_today():
        print("Report already sent today — skipping retry.")
        sys.exit(0)

    print(f"  Centre: {LAT_CENTRE}N, {abs(LON_CENTRE)}W  Radius: {RADIUS_KM}km")
    print("  Fetching NOTAMs from NATS AIS...")
    notams = fetch_notams()

    print("  Building Slack message...")
    payload = build_slack_message(notams)

    print("  Posting to #notams...")
    post_to_slack(payload)

    mark_sent_today()
    print("GOSH NOTAM report posted successfully to #notams.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
