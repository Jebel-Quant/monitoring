#!/usr/bin/env python3
"""Validate the dashboard JSON before Grafana silently mis-renders it.

Panel ids must be unique - `viewPanel=<id>` links resolve by id, and cloning a
panel in a patch script is an easy way to duplicate one. Every link target must
also exist, or a tile click lands on an empty page.
"""

import json
import pathlib
import sys

path = pathlib.Path(__file__).resolve().parent.parent / "grafana/dashboards/fleet.json"
dash = json.loads(path.read_text())


def walk(panels):
    for panel in panels:
        yield panel
        yield from walk(panel.get("panels", []))


panels = list(walk(dash["panels"]))
ids = [p["id"] for p in panels]
problems = []

for pid in {i for i in ids if ids.count(i) > 1}:
    titles = ", ".join(repr(p.get("title")) for p in panels if p["id"] == pid)
    problems.append(f"duplicate panel id {pid}: {titles}")

targets = {
    int(link["url"].split("viewPanel=")[1].split("&")[0])
    for p in panels
    for link in p.get("fieldConfig", {}).get("defaults", {}).get("links", [])
    if "viewPanel=" in link.get("url", "")
}
for missing in sorted(targets - set(ids)):
    problems.append(f"link points at viewPanel={missing}, which no panel has")

# Panels are placed on a 24-column grid by hand in patch scripts; two panels
# claiming the same cells makes Grafana shuffle them unpredictably on load.
placed = [p for p in dash["panels"]]


def overlaps(a, b):
    return not (
        a["x"] + a["w"] <= b["x"]
        or b["x"] + b["w"] <= a["x"]
        or a["y"] + a["h"] <= b["y"]
        or b["y"] + b["h"] <= a["y"]
    )


for i, first in enumerate(placed):
    for second in placed[i + 1 :]:
        if overlaps(first["gridPos"], second["gridPos"]):
            problems.append(
                f"panels overlap on the grid: {first.get('title')!r} and {second.get('title')!r}"
            )

for problem in problems:
    print(f"  FAIL {problem}")
if problems:
    sys.exit(1)
print(
    f"  ok: {len(panels)} panels, ids unique, {len(targets)} link targets resolve, "
    f"{len(placed)} placed with no overlaps"
)
