#!/usr/bin/env python3
"""
Southwark NOTAM Report — weekday 7:45am Slack poster
Centre: 2-6 Boundary Row, Southwark SE1 8HP (51.5023N, 0.1055W)
Radius: 2.5km — haversine filtering on precise body coordinates
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
from datetime import datetime, date

# ── CONFIG ───────────────────────────────────────────────────────────────────
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

GEOAPIFY_KEY = "85af2442ac73422ab11407c17d344789"

# Centre: 2-6 Boundary Row SE1 8HP
LAT_CENTRE = 51.5023
LON_CENTRE = -0.1055
RADIUS_KM  = 2.5

# Matternet TRA EGTR196 boundary (for map overlay via GeoJSON)
TRA_POLYGON = [
    [-0.120000, 51.501111],
    [-0.086111, 51.503889],
    [-0.085556, 51.500833],
    [-0.120556, 51.497778],
    [-0.120000, 51.501111],
]

NATS_FEEDS = [
    "http://pibs.nats.co.uk/operational/pibs/pib3.shtml",
    "http://pibs.nats.co.uk/operational/pibs/pib51n.shtml",
    "http://pibs.nats.co.uk/operational/pibs/pib5.shtml",
    "http://pibs.nats.co.uk/operational/pibs/pib6.shtml",
]

STATE_FILE  = os.path.expanduser("~/scripts/notam_report_sent.txt")
NOTAM_ID_RE = re.compile(r'^([A-Z]\d{4}/\d{2,4})$')
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
    In the NATS PIB HTML the NOTAM ID appears AFTER its body text, not before.
    Structure: [body of NOTAM X] [ID of NOTAM X] [body of NOTAM X+1] [ID of NOTAM X+1]
    So we accumulate text and assign it to the ID that follows it.
    """
    notams = []
    pending_parts = []

    for block in blocks:
        stripped = block.strip()
        if NOTAM_ID_RE.match(stripped):
            # Text accumulated so far belongs to THIS NOTAM ID
            notams.append({
                "id":   stripped,
                "text": " ".join(pending_parts).strip()
            })
            pending_parts = []
        else:
            pending_parts.append(block)

    # Trailing text after last ID — attach to last NOTAM
    if notams and pending_parts:
        notams[-1]["text"] = (notams[-1]["text"] + " " + " ".join(pending_parts)).strip()

    # Drop entries with no meaningful body
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
        lat_s, lon_s = m.group(1), m.group(2)
        lat = int(lat_s[:2]) + int(lat_s[2:4]) / 60.0 + int(lat_s[4:]) / 3600.0
        lon = -(int(lon_s[:3]) + int(lon_s[3:5]) / 60.0 + int(lon_s[5:]) / 3600.0)
        results.append((lat, lon))
    if not results:
        for m in re.finditer(r'(\d{4})N(\d{5})W', text):
            lat_s, lon_s = m.group(1), m.group(2)
            lat = int(lat_s[:2]) + int(lat_s[2:]) / 60.0
            lon = -(int(lon_s[:3]) + int(lon_s[3:]) / 60.0)
            results.append((lat, lon))
    return results


def within_radius(text):
    best_dist = None
    best_coord = None
    for lat, lon in parse_coords(text):
        dist = haversine_km(LAT_CENTRE, LON_CENTRE, lat, lon)
        if dist <= RADIUS_KM:
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_coord = (lat, lon)
    return best_coord, best_dist


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
                coord, dist = within_radius(full_text)
                if coord is None:
                    continue
                seen_ids.add(n["id"])
                notams.append({
                    "id":          n["id"],
                    "text":        n["text"][:500],
                    "dist":        round(dist, 2),
                    "coord":       coord,
                    "active_today": is_active_today(n["text"]),
                })

        except Exception as e:
            print(f"  Warning: {url} failed — {e}", file=sys.stderr)

    notams.sort(key=lambda x: x["dist"])
    print(f"  Total within {RADIUS_KM}km: {len(notams)}")
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


def marker_color(notam):
    t = notam["text"].upper()
    if any(k in t for k in ["TDA", "RESTRICTED", "PROHIBITED", "DANGER"]):
        return "red"
    elif any(k in t for k in ["TRA", "UAS", "DRONE", "BVLOS", "RPAS"]):
        return "orange"
    else:
        return "yellow"


def build_map_url(notams):
    """
    Geoapify static map with TRA polygon + numbered NOTAM markers.
    Returns (url, legend_lines) where legend_lines maps number -> NOTAM ID.
    """
    import json as _json, urllib.parse as _up

    geojson = {
        "type": "Feature",
        "properties": {
            "linecolor": "#dc2626", "linewidth": 2, "lineopacity": 0.9,
            "fillcolor": "#dc2626", "fillopacity": 0.2
        },
        "geometry": {"type": "Polygon", "coordinates": [TRA_POLYGON]}
    }
    geojson_enc = _up.quote(_json.dumps(geojson, separators=(",", ":")), safe="")

    SEP = "%7C"
    color_map = {"red": "#dc2626", "orange": "#f97316", "yellow": "#eab308"}

    # HQ star marker
    markers = [f"lonlat:{LON_CENTRE},{LAT_CENTRE};color:#6366f1;size:medium;icon:star"]
    legend = []

    for i, n in enumerate(notams[:14], start=1):
        lat, lon = n["coord"]
        color = color_map.get(marker_color(n), "#888888")
        markers.append(
            f"lonlat:{lon:.5f},{lat:.5f};color:{color};size:medium;text:{i}"
        )
        legend.append(n["id"])  # unused in message but kept for reference

    markers_enc = SEP.join(_up.quote(m, safe=";:,.-") for m in markers)

    url = (
        f"https://maps.geoapify.com/v1/staticmap"
        f"?style=osm-bright&width=800&height=400"
        f"&center=lonlat:{LON_CENTRE},{LAT_CENTRE}&zoom=13"
        f"&geojson={geojson_enc}"
        f"&marker={markers_enc}"
        f"&apiKey={GEOAPIFY_KEY}"
    )
    return url, legend

JAM_QCODES   = ['QGPXX', 'QGPAS', 'QGPWS', 'QNAVW', 'QNVXX']
JAM_KEYWORDS = ['JAM', 'GNSS', 'GPS SIGNAL', 'GPS/GNSS', 'GPS UNRELIABLE',
                'INTERFERENCE', 'SIGNAL UNRELIABLE', 'GPS OUTAGE']


def is_jamming_notam(text):
    """Return True if NOTAM relates to GPS/GNSS jamming or interference."""
    t = text.upper()
    return any(qc in t for qc in JAM_QCODES) or any(kw in t for kw in JAM_KEYWORDS)


def sanitise_for_slack(text):
    """Escape characters that break Slack mrkdwn block validation."""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def build_slack_message(notams):
    today = datetime.now().strftime("%A %d %B %Y")
    area  = "2-6 Boundary Row, Southwark SE1 8HP  |  2.5km radius"

    if not notams:
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": f"NOTAM Report - {today}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Area:* {area}"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*No active NOTAMs within 2.5km.* Airspace is clear."}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Source: NATS AIS UK PIB  |  Mon-Fri 07:45"}]}
        ]
        return {"text": f"NOTAM Report - {today} | No active NOTAMs", "blocks": blocks}

    count    = len(notams)
    cap_note = f" _(showing 10 of {count})_" if count > 10 else ""

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"NOTAM Report - {today}"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Area*\n{area}"},
                {"type": "mrkdwn", "text": f"*Active NOTAMs*\n{count}{cap_note}"}
            ]
        },
        {"type": "divider"}
    ]

    # GPS jamming warning — inserted above map if any jamming NOTAMs active today
    jamming = [n for n in notams if is_jamming_notam(n["text"]) and n.get("active_today")]
    if jamming:
        jam_ids = "  ".join(n["id"] for n in jamming)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": (
                "🚨 *OPERATIONAL DISRUPTION — GPS/GNSS JAMMING ACTIVE*\n"
                "GNSS signals may be unreliable in your area. UAS operations affected.\n"
                f"_{jam_ids}_"
            )}
        })
        blocks.append({"type": "divider"})

    # Static map image block with numbered legend
    map_url, legend = build_map_url(notams)
    print(f"  Map URL: {map_url[:80]}...")
    print(f"  Legend: {legend}")
    pass  # DROPPED IMAGE:     blocks.append({"type": "image", "image_url": map_url, "alt_text": "NOTAM area map"})
    if legend:
        key = "  ".join(f"{i+1}={nid.split('/')[0]}" for i, nid in enumerate(legend))
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"_{key}_"}
        })
    blocks.append({"type": "divider"})

    for i, n in enumerate(notams[:10], start=1):
        emoji, label  = classify_notam(n["text"])
        dist_str      = f"  _{n['dist']}km_"
        today_tag     = "  🔔 *TODAY*" if n.get("active_today") else ""
        detail        = sanitise_for_slack(n["text"][:300] + ("..." if len(n["text"]) > 300 else ""))
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{i}.* {emoji} *{n['id']}*  `{label}`{dist_str}{today_tag}\n{detail}"}
        })
        blocks.append({"type": "divider"})

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "🔔 TODAY = active today  |  Red polygon = Matternet TRA EGTR196  |  Source: NATS AIS  |  Mon-Fri 07:45"}]
    })

    return {
        "text": f"NOTAM Report - {today} | {count} active NOTAM{'s' if count != 1 else ''}",
        "blocks": blocks
    }


def post_to_slack(payload):
    # Write debug file FIRST so we can inspect even if post fails
    debug_path = os.path.expanduser("~/scripts/notam_payload_debug.json")
    with open(debug_path, "w", encoding="utf-8") as dbg:
        json.dump(payload, dbg, indent=2, ensure_ascii=False)

    # Validate each block and print any suspicious ones
    for i, block in enumerate(payload.get("blocks", [])):
        btype = block.get("type", "")
        if btype == "section":
            txt = block.get("text", {}).get("text", "")
            if len(txt) > 3000:
                print(f"  WARNING block {i}: section text too long ({len(txt)} chars)")
            for ch in ["&", "<", ">"]:
                if ch in txt:
                    print(f"  WARNING block {i}: raw '{ch}' in section text: {repr(txt[:80])}")
        elif btype == "image":
            url = block.get("image_url", "")
            if "|" in url:
                print(f"  WARNING block {i}: raw pipe in image URL")
            if not block.get("alt_text"):
                print(f"  WARNING block {i}: missing alt_text")
        elif btype == "context":
            for el in block.get("elements", []):
                txt = el.get("text", "")
                for ch in ["&", "<", ">"]:
                    if ch in txt:
                        print(f"  WARNING block {i} context: raw '{ch}': {repr(txt[:80])}")

    data = json.dumps(payload).encode("utf-8")
    print(f"  Payload: {len(data)} bytes, {len(payload.get('blocks',[]))} blocks")
    req  = urllib.request.Request(
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
    print(f"[{datetime.now().strftime('%H:%M:%S')}] NOTAM report starting...")

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
    print("Report posted successfully to #notams.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
