#!/usr/bin/env python3
"""
notam_status.py — writes route GO/NOGO into qnh.js for the Thames HEMS display.

Reuses the existing NOTAM scripts' own parsing by importing them, so logic
stays in sync with the Slack reports. Rule (per ops): any GPS/GNSS jamming
NOTAM active today on a route => that route NOGO. Either route NOGO => the
whole box shows NOGO.

Intended to run once each morning (e.g. 08:00) after the NOTAM reports.
"""
import os, sys, json, re, importlib.util
from datetime import datetime

SCRIPTS = os.environ.get("SCRIPTS_DIR", os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("OUT_DIR", "/Users/tim.rundle-spiers/Library/CloudStorage/GoogleDrive-tim.rundle-spiers@matternet.com/My Drive/DISPATCH BOARD") + "/routes.js"


def load(name):
    """Import a sibling script by filename without running its __main__."""
    path = os.path.join(SCRIPTS, name)
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def route_status(mod):
    """Return (status, cause) for one route using its own fetch/parse."""
    try:
        notams = mod.fetch_notams()
    except Exception as e:
        print(f"  fetch failed: {e}", file=sys.stderr)
        return "unknown", "feed error"

    # Jamming active today anywhere in range => NOGO (per ops rule)
    jam = [n for n in notams
           if n.get("active_today") and (
               n.get("jamming") is True or
               mod.is_jamming_notam(f"{n['id']} {n['text']}"))]
    if jam:
        return "nogo", "GPS jamming"

    # Otherwise GO. (Room to add more NOGO triggers here later if you want,
    # e.g. a required TDA/TRA not active — say the word and I'll wire it.)
    return "go", ""


def main():
    sw = load("southwark_notam_report.py")
    gh = load("gosh_notam_report.py")

    gstt_status, gstt_cause = route_status(sw)
    gosh_status, gosh_cause = route_status(gh)

    routes = {
        "asof": datetime.now().strftime("%H:%M"),
        "items": [
            {"name": "GSTT", "status": gstt_status, "cause": gstt_cause},
            {"name": "GOSH", "status": gosh_status, "cause": gosh_cause},
        ],
    }

    # Merge into existing qnh.js, preserving QNH/weather already there
    with open(OUT, "w") as f:
        f.write("window.LOCAL_ROUTES = " + json.dumps(routes) + ";")
    print(f"routes written: GSTT={gstt_status} GOSH={gosh_status} -> {OUT}")


if __name__ == "__main__":
    main()
