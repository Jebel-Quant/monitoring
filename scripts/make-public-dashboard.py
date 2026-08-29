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


GOOD, WARN, CRIT = "#0ca30c", "#fab219", "#d03b3b"


def mission_control(dash: dict) -> None:
    """Restyle the public copy as a status board rather than a working tool.

    The private board is something you interrogate; this one is something you
    glance at. So: one dominant verdict, a row of plain counters, and a row of
    lit status blocks. Colour does real work here - every coloured panel means
    good or bad, and the counters that mean neither stay unlit, because a
    backlog of 37 issues is workload, not a fault.
    """
    by_title = {p.get("title"): p for p in dash["panels"]}

    def restyle(title, *, lit, unit=None):
        panel = by_title.get(title)
        if not panel:
            return
        opts = panel.setdefault("options", {})
        opts["colorMode"] = "background" if lit else "none"
        opts["graphMode"] = "area"          # a sparkline turns a number into telemetry
        # "value_and_name" prints the raw PromQL beside the number - the series
        # has no name, so Grafana falls back to the expression. The panel title
        # already says what it is.
        opts["textMode"] = "value"
        opts["justifyMode"] = "center"
        opts.setdefault("reduceOptions", {})["calcs"] = ["lastNotNull"]
        opts["text"] = {"valueSize": 56 if lit else 44, "titleSize": 13}
        if unit:
            panel["fieldConfig"]["defaults"]["unit"] = unit

    hero = {
        "id": 200,
        "type": "stat",
        "title": "FLEET STATUS",
        "description": "Every red workflow, drifted repo and failing pull request, added up.",
        "datasource": {"type": "prometheus", "uid": "jq-prometheus"},
        "targets": [{
            "refId": "A",
            "expr": "sum(jq_ci_last_run_success == bool 0) "
                    "+ sum(jq_rhiza_releases_behind > bool 0) "
                    "+ sum(jq_open_pull_requests_failing)",
            "instant": True,
        }],
        "options": {
            "colorMode": "background",
            "graphMode": "area",
            "textMode": "value",
            "justifyMode": "center",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "text": {"valueSize": 110},
        },
        "fieldConfig": {
            "defaults": {
                "unit": "short",
                "decimals": 0,
                "mappings": [{"type": "value", "options": {
                    "0": {"text": "ALL CLEAR", "index": 0}}}],
                "thresholds": {"mode": "absolute", "steps": [
                    {"color": GOOD, "value": None},
                    {"color": WARN, "value": 1},
                    {"color": CRIT, "value": 5},
                ]},
            },
            "overrides": [],
        },
    }
    dash["panels"].append(hero)
    by_title["FLEET STATUS"] = hero

    for t in ("Repos monitored", "Open PRs", "Open issues", "Data age"):
        restyle(t, lit=False, unit="s" if t == "Data age" else None)
    for t in ("CI red on main", "Behind template", "PRs with red checks"):
        restyle(t, lit=True)
    # Data age is the one counter that IS a verdict - stale data invalidates
    # everything above it - so it keeps its thresholds and gets lit.
    if "Data age" in by_title:
        by_title["Data age"]["options"]["colorMode"] = "background"

    LAYOUT = {
        "FLEET STATUS": (0, 1, 9, 8),
        "Repos monitored": (9, 1, 5, 4),
        "Open PRs": (14, 1, 5, 4),
        "Open issues": (19, 1, 5, 4),
        "CI red on main": (9, 5, 5, 4),
        "Behind template": (14, 5, 5, 4),
        "PRs with red checks": (19, 5, 5, 4),
        "Data age": (0, 9, 24, 3),  # a thin status strip closing the header
    }
    placed = set(LAYOUT)
    for title, (x, y, w, h) in LAYOUT.items():
        if title in by_title:
            by_title[title]["gridPos"] = {"x": x, "y": y, "w": w, "h": h}

    # Everything below the header block shifts to sit under it.
    header_bottom = 12
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
    if LOCAL in raw:
        raise SystemExit("local-clone metrics survived the strip")
    if "/d/jq-fleet" in raw:
        raise SystemExit("an absolute dashboard link survived - it would 401 a public viewer")

    DST.write_text(json.dumps(json.loads(raw), indent=2) + "\n")
    print(f"wrote {DST.relative_to(HERE)} ({len(dropped_ids)} local panels dropped)")


if __name__ == "__main__":
    main()
