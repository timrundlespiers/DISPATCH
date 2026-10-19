import re
#!/usr/bin/env python3
"""
notam_status.py — writes route GO/NOGO into routes.js for the dispatch board.

Rule (per ops):
 - GPS/GNSS jamming active today on a route => that route NOGO.
 - Our own TDA/TRA must be active today, else NOGO. Our area is matched by
   designator against the raw NATS feeds, so it is found even when its centre
   sits outside the route's capture radius.
"""
import os, sys, json, re, importlib.util
from datetime import datetime

SCRIPTS = os.environ.get("SCRIPTS_DIR", os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("OUT_DIR", "/Users/tim.rundle-spiers/Library/CloudStorage/GoogleDrive-tim.rundle-spiers@matternet.com/My Drive/DISPATCH BOARD") + "/routes.js"


def load(name):
    path = os.path.join(SCRIPTS, name)
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _own_area_active(mod, designators):
    """True if one of OUR designators is active today, checked against the raw
    feeds (no radius filter) using the module's own parser."""
    try:
        Parser = mod.NotamHTMLParser
        group  = mod.group_notams
        active = mod.is_active_today
        fetch  = mod.fetch_url
        feeds  = mod.NATS_FEEDS
    except Exception:
        return False
    for url in feeds:
        try:
            html = fetch(url)
        except Exception:
            continue
        if not any(d.upper() in html.upper() for d in designators):
            continue
        try:
            p = Parser(); p.feed(html)
            for n in group(p.get_blocks()):
                t = n["text"].upper()
                if any(d.upper() in t for d in designators) and active(n["text"]):
                    return True
        except Exception:
            return True   # designator present in a current feed; assume active
    return False


def _area_active(notams, designators):
    for n in notams:
        if not n.get("active_today"):
            continue
        t = n["text"].upper()
        if any(d.upper() in t for d in designators):
            return True
    return False


def route_status(mod, designators):
    """Return (status, cause) for one route."""
    try:
        notams = mod.fetch_notams()
    except Exception as e:
        print(f"  fetch failed: {e}", file=sys.stderr)
        return "unknown", "feed error"

    # Jamming active today => NOGO
    jam = [n for n in notams
           if n.get("active_today") and (
               n.get("jamming") is True or
               mod.is_jamming_notam(f"{n['id']} {n['text']}"))]
    if jam:
        return "nogo", "jamming"

    # Our TDA/TRA must be active (radius-filtered OR raw-feed designator match)
    if not (_area_active(notams, designators) or _own_area_active(mod, designators)):
        return "nogo", "area not active"

    return "go", ""


def main():
    sw = load("southwark_notam_report.py")
    gh = load("gosh_notam_report.py")

    gstt_status, gstt_cause = route_status(sw, ["EGTR196", "EGD196A"])
    gosh_status, gosh_cause = route_status(gh, ["EGD174A"])

    routes = {
        "asof": datetime.now().strftime("%H:%M"),
        "items": [
            {"name": "GSTT", "status": gstt_status, "cause": gstt_cause},
            {"name": "GOSH", "status": gosh_status, "cause": gosh_cause},
        ],
    }
    with open(OUT, "w") as f:
        f.write("window.LOCAL_ROUTES = " + json.dumps(routes) + ";")
    print(f"routes written: GSTT={gstt_status} GOSH={gosh_status} -> {OUT}")


if __name__ == "__main__":
    main()
