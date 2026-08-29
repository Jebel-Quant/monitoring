#!/usr/bin/env python3
"""Generate the public copy of the fleet board from the private one.

Two things have to change for a board that will be served world-readable:

1. Grafana's public dashboards do not resolve template variables, so every
   panel filtered on `{repo=~"$repo"}` renders "No data" behind a public link.
   The variable and its selectors are stripped; the queries still mean the same
   thing because the collector runs with JQ_PUBLIC_ONLY.

2. Everything sourced from `jq_local_*` describes *one particular machine* -
   its dirty files, its checked-out branch names, how long since it fetched.
   That is not secret when the repos are public, but it is someone's working
   state and it is meaningless to anyone else, so it is dropped.

Run after changing fleet.json; the output is provisioned like any other file.
"""

import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent.parent
SRC = HERE / "grafana/dashboards/fleet.json"
DST = HERE / "grafana/dashboards/fleet-public.json"

LOCAL = "jq_local"


def targets_of(panel):
    return panel.get("targets", []) or []


def is_local_only(panel):
    """True when every query in the panel comes from local-clone metrics."""
    exprs = [t.get("expr", "") for t in targets_of(panel)]
    return bool(exprs) and all(LOCAL in e for e in exprs)


def main() -> None:
    dash = json.loads(SRC.read_text())
    dash["uid"] = "jq-fleet-public"
    dash["title"] = "Jebel-Quant Fleet (public)"
    dash["description"] = (
        "Public copy of the fleet board. Public repos only, and nothing about any "
        "particular working copy - the collector runs with JQ_PUBLIC_ONLY and the "
        "local-clone panels are stripped from this copy."
    )
    dash["templating"] = {"list": []}
    dash["tags"] = sorted(set(dash.get("tags", [])) | {"public"})

    def walk(panels):
        for panel in panels:
            yield panel
            yield from walk(panel.get("panels", []))

    dropped_ids = {p["id"] for p in walk(dash["panels"]) if is_local_only(p)}

    def prune(panels):
        kept = []
        for panel in panels:
            if panel.get("id") in dropped_ids:
                continue
            if panel.get("panels"):
                panel["panels"] = prune(panel["panels"])
            # A mixed panel keeps its non-local series only.
            if targets_of(panel) and not is_local_only(panel):
                keep = [t for t in targets_of(panel) if LOCAL not in t.get("expr", "")]
                if len(keep) != len(targets_of(panel)):
                    gone = {
                        t.get("legendFormat")
                        for t in targets_of(panel)
                        if LOCAL in t.get("expr", "")
                    }
                    panel["targets"] = keep
                    panel["fieldConfig"]["overrides"] = [
                        o
                        for o in panel["fieldConfig"].get("overrides", [])
                        if o["matcher"].get("options") not in gone
                    ]
            # Links into a dropped panel would land on an empty page.
            links = panel.get("fieldConfig", {}).get("defaults", {}).get("links")
            if links:
                panel["fieldConfig"]["defaults"]["links"] = [
                    ln
                    for ln in links
                    if not any(f"viewPanel={i}" in ln.get("url", "") for i in dropped_ids)
                ]
            kept.append(panel)
        return kept

    dash["panels"] = prune(dash["panels"])

    # Repack. Removing panels leaves holes, and Grafana's compaction on a
    # public dashboard is not something to rely on. Panels that shared a y sit
    # side by side, so bands are preserved and only re-stacked - rebuilding
    # from scratch would turn a row of tiles into a column of them.
    top = dash["panels"]
    order: list[tuple[int, list]] = []
    for panel in sorted(top, key=lambda x: (x["gridPos"]["y"], x["gridPos"]["x"])):
        if order and order[-1][0] == panel["gridPos"]["y"]:
            order[-1][1].append(panel)
        else:
            order.append((panel["gridPos"]["y"], [panel]))

    y = 0
    for _, band in order:
        height = max(x["gridPos"]["h"] for x in band)
        # Close horizontal gaps left by dropped tiles, preserving order/width.
        x_cursor = 0
        for panel in band:
            panel["gridPos"]["y"] = y
            if panel.get("type") != "row":
                panel["gridPos"]["x"] = x_cursor
                x_cursor += panel["gridPos"]["w"]
        for panel in band:
            for i, child in enumerate(panel.get("panels", [])):
                child["gridPos"]["y"] = y + 1 + i * child["gridPos"]["h"]
        y += height

    raw = json.dumps(dash)
    raw = raw.replace('{repo=~\\"$repo\\", ', "{")
    raw = raw.replace('{repo=~\\"$repo\\"}', "")
    raw = raw.replace(
        "/d/jq-fleet/jebel-quant-fleet?", "/d/jq-fleet-public/jebel-quant-fleet-public?"
    )

    leftover = re.findall(r"\$repo", raw)
    if leftover:
        raise SystemExit(f"{len(leftover)} unresolved $repo references remain")
    if LOCAL in raw:
        raise SystemExit("local-clone metrics survived the strip")

    DST.write_text(json.dumps(json.loads(raw), indent=2) + "\n")
    print(f"wrote {DST.relative_to(HERE)} ({len(dropped_ids)} local panels dropped)")


if __name__ == "__main__":
    main()
