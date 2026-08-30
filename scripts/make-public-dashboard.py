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

3. Everything sourced from `jq_rhiza_*` is template drift, which is a rhiza
   concern. Most people who open the public link do not use the template at
   all, so "three releases behind" reads as a defect rather than as something
   that does not apply to them. The whole Template drift section goes, along
   with the drill-down table and the trend-chart series that read the same
   metrics - a headline tile removed while a chart still plots the number is
   not a removal. The private board keeps all of it.

Run after changing fleet.json; the output is provisioned like any other file.
"""

import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent.parent
SRC = HERE / "grafana/dashboards/fleet.json"
DST = HERE / "grafana/dashboards/fleet-public.json"

# Metric families the public copy does not carry, each for its own reason
# (see 2 and 3 above). A panel is dropped when every one of its queries reads
# one of these; a panel that also reads something else keeps its other series.
DROPPED_METRICS = ("jq_local", "jq_rhiza")


def targets_of(panel):
    return panel.get("targets", []) or []


def is_dropped_metric(expr: str) -> bool:
    return any(metric in expr for metric in DROPPED_METRICS)


def is_dropped_only(panel):
    """True when every query in the panel reads a family we do not publish."""
    exprs = [t.get("expr", "") for t in targets_of(panel)]
    return bool(exprs) and all(is_dropped_metric(e) for e in exprs)


def drop_empty_rows(panels):
    """A row whose every panel was dropped would render as a bare heading.

    Template drift loses all three of its panels, so the row goes with them
    rather than leaving a title with nothing under it.
    """
    ordered = sorted(panels, key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"]))
    kept, i = [], 0
    while i < len(ordered):
        panel = ordered[i]
        if panel.get("type") != "row":
            kept.append(panel)
            i += 1
            continue
        j = i + 1
        following = []
        while j < len(ordered) and ordered[j].get("type") != "row":
            following.append(ordered[j])
            j += 1
        # Children live inside a collapsed row and beside an expanded one.
        if panel.get("panels") or following:
            kept.append(panel)
            kept.extend(following)
        i = j
    return kept


def mission_control(dash: dict) -> None:
    """Restyle the public copy as an instrument panel rather than a working tool.

    Restrained on purpose. An earlier version led with a full-bleed green
    "ALL CLEAR" block, which reads as a billboard rather than a status board and
    drowns the numbers that actually carry information. Colour stays on the
    figure, not behind it: a dark panel with a lit number is legible at a glance
    without shouting, and a wall of green tells you nothing a row of green
    numerals does not.
    """
    by_title = {p.get("title"): p for p in dash["panels"]}

    def restyle(title, *, lit, unit=None):
        panel = by_title.get(title)
        if not panel:
            return
        opts = panel.setdefault("options", {})
        # "value" colours the numeral against the dark surface; "background"
        # floods the panel. "value_and_name" would print the raw PromQL beside
        # it, since the series has no name and Grafana falls back to the query.
        # Only verdicts wear colour. A count of repos or open issues is
        # workload, not good news - colouring it green says something untrue.
        opts["colorMode"] = "value" if lit else "none"
        opts["graphMode"] = "area"      # a sparkline turns a number into telemetry
        opts["textMode"] = "value"
        opts["justifyMode"] = "auto"
        opts.setdefault("reduceOptions", {})["calcs"] = ["lastNotNull"]
        opts["text"] = {"valueSize": 48, "titleSize": 13}
        if unit:
            panel["fieldConfig"]["defaults"]["unit"] = unit

    for t in ("Repos monitored", "Open PRs", "Open issues"):
        restyle(t, lit=False)
    for t in ("CI red on main", "PRs with red checks"):
        restyle(t, lit=True)
    # Data age is a verdict: stale data invalidates everything above it.
    restyle("Data age", lit=True, unit="s")

    # "Behind template" is deliberately absent: it lives in the Template drift
    # section at the bottom, not in the header. Most people reading this board
    # do not use the template at all, and a headline tile implies otherwise.
    # Listing it here would pin it back into the header - `placed` titles are
    # positioned absolutely, everything else keeps its order from fleet.json.
    LAYOUT = {
        "Repos monitored": (0, 1, 5, 5),
        "Open PRs": (5, 1, 5, 5),
        "Open issues": (10, 1, 5, 5),
        "CI red on main": (15, 1, 5, 5),
        "PRs with red checks": (20, 1, 4, 5),
        "Data age": (0, 6, 24, 3),
    }
    placed = set(LAYOUT)
    for title, (x, y, w, h) in LAYOUT.items():
        if title in by_title:
            by_title[title]["gridPos"] = {"x": x, "y": y, "w": w, "h": h}

    header_bottom = 9
    rest = [p for p in dash["panels"]
            if p.get("title") not in placed and p["gridPos"]["y"] > 0]
    if rest:
        delta = header_bottom - min(p["gridPos"]["y"] for p in rest)
        for panel in rest:
            panel["gridPos"]["y"] += delta
    dash["panels"].sort(key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"]))


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

    dropped_ids = {p["id"] for p in walk(dash["panels"]) if is_dropped_only(p)}

    def prune(panels):
        kept = []
        for panel in panels:
            if panel.get("id") in dropped_ids:
                continue
            if panel.get("panels"):
                panel["panels"] = prune(panel["panels"])
            # A mixed panel keeps its non-local series only.
            if targets_of(panel) and not is_dropped_only(panel):
                keep = [t for t in targets_of(panel) if not is_dropped_metric(t.get("expr", ""))]
                if len(keep) != len(targets_of(panel)):
                    gone = {
                        t.get("legendFormat")
                        for t in targets_of(panel)
                        if is_dropped_metric(t.get("expr", ""))
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

    dash["panels"] = drop_empty_rows(prune(dash["panels"]))

    # Strip every data link: none of them can work for an anonymous viewer.
    for panel in walk(dash["panels"]):
        defaults = panel.get("fieldConfig", {}).get("defaults", {})
        defaults.pop("links", None)

    # Expand collapsed rows so their tables are simply visible. A visitor has
    # no way to know a tile is clickable, and clicking is what breaks - so the
    # detail is shown inline rather than hidden behind a link that cannot work.
    #
    # Layout is rebuilt from scratch afterwards. Each panel is tagged with the
    # band it belongs to: panels that shared a y in the source stay side by
    # side, while a row's freed children each get a band of their own. Trying to
    # preserve the original y values instead collides, because two sections end
    # up claiming the same one.
    ordered: list[tuple[int, dict]] = []
    band = 0
    for panel in sorted(dash["panels"], key=lambda x: (x["gridPos"]["y"], x["gridPos"]["x"])):
        if panel.get("type") == "row":
            children = panel.pop("panels", []) or []
            panel["collapsed"] = False
            panel["panels"] = []
            if panel.get("title", "").startswith("Drill-down"):
                panel["title"] = "Detail by repo"
            band += 1
            ordered.append((band, panel))
            for child in children:
                band += 1
                ordered.append((band, child))
        else:
            band += 1
            ordered.append((band, panel))

    # Re-group: consecutive non-row panels whose widths fit one row share a band.
    regrouped: list[list[dict]] = []
    for _, panel in ordered:
        if panel.get("type") == "row" or not regrouped:
            regrouped.append([panel])
            continue
        current = regrouped[-1]
        if current[0].get("type") == "row":
            regrouped.append([panel])
        elif sum(x["gridPos"]["w"] for x in current) + panel["gridPos"]["w"] <= 24:
            current.append(panel)
        else:
            regrouped.append([panel])

    y = 0
    final: list[dict] = []
    for group in regrouped:
        x = 0
        height = max(g["gridPos"]["h"] for g in group)
        for panel in group:
            panel["gridPos"] = {**panel["gridPos"], "x": x, "y": y}
            x += panel["gridPos"]["w"]
            final.append(panel)
        y += height
    dash["panels"] = final

    mission_control(dash)

    raw = json.dumps(dash)
    raw = raw.replace('{repo=~\\"$repo\\", ', "{")
    raw = raw.replace('{repo=~\\"$repo\\"}', "")
    # No drill-down links at all on the public copy. A public dashboard lives
    # at /public-dashboards/<token>, a path unknown when this file is generated:
    # an absolute /d/... link is an authenticated route and sends the visitor to
    # a login page, and a relative "?viewPanel=" link is resolved by Grafana's
    # router against the app root, landing on "/" instead. Since the detail
    # cannot be reached by clicking, it is shown inline instead - see below.
    raw = raw.replace("/d/jq-fleet/jebel-quant-fleet?", "?")

    leftover = re.findall(r"\$repo", raw)
    if leftover:
        raise SystemExit(f"{len(leftover)} unresolved $repo references remain")
    for metric in DROPPED_METRICS:
        if metric in raw:
            raise SystemExit(f"{metric} metrics survived the strip")
    if "/d/jq-fleet" in raw:
        raise SystemExit("an absolute dashboard link survived - it would 401 a public viewer")

    DST.write_text(json.dumps(json.loads(raw), indent=2) + "\n")
    print(f"wrote {DST.relative_to(HERE)} ({len(dropped_ids)} panels dropped)")


if __name__ == "__main__":
    main()
