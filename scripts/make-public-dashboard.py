#!/usr/bin/env python3
"""Generate the public copy of the fleet board from the private one.

Grafana's public dashboards do not resolve template variables, so every panel
filtered on `{repo=~"$repo"}` renders "No data" behind a public link. This
strips the variable and its selectors, leaving queries that mean the same thing
because the collector is already restricted to public repos by JQ_PUBLIC_ONLY.

Run after changing fleet.json; the output is provisioned like any other file.
"""

import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent.parent
SRC = HERE / "grafana/dashboards/fleet.json"
DST = HERE / "grafana/dashboards/fleet-public.json"

dash = json.loads(SRC.read_text())
dash["uid"] = "jq-fleet-public"
dash["title"] = "Jebel-Quant Fleet (public)"
dash["description"] = (
    "Public copy of the fleet board. Covers public repos only - the collector "
    "runs with JQ_PUBLIC_ONLY, so private repos are never gathered."
)
dash["templating"] = {"list": []}
dash["tags"] = sorted(set(dash.get("tags", [])) | {"public"})

raw = json.dumps(dash)
# `{repo=~"$repo", conclusion!~...}` -> `{conclusion!~...}`; a lone selector goes entirely.
raw = raw.replace('{repo=~\\"$repo\\", ', "{")
raw = raw.replace('{repo=~\\"$repo\\"}', "")
# Drill-down links must stay inside the public dashboard.
raw = raw.replace("/d/jq-fleet/jebel-quant-fleet?", "/d/jq-fleet-public/jebel-quant-fleet-public?")

leftover = re.findall(r"\$repo", raw)
if leftover:
    raise SystemExit(f"{len(leftover)} unresolved $repo references remain - fix the stripper")

DST.write_text(json.dumps(json.loads(raw), indent=2) + "\n")
print(f"wrote {DST.relative_to(HERE)}")
