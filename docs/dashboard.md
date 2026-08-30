# The dashboard

## Clicking a tile

Every tile except *Repos monitored* links to a table listing exactly what is
behind that number — collapsed under *Drill-down* at the bottom, opened full
screen by the click. Each drill-down's row count is the tile's value by
construction, and each repo name links to GitHub.

The tiles are laid out in two rows because that is how the data splits. The top
row is what **GitHub** says about the repos — a headline count, the two workload
counts (open PRs, open issues), then the three problem counts. The bottom row is
what **this machine** says about the clones: not in sync, dirty, off default
branch. A green top row with an amber bottom row means the fleet is healthy and
your checkouts are stale — a different problem, and a much cheaper one.

*Open PRs* and *Open issues* are deliberately **not** status-coloured. They are
workload, not verdicts; the status palette is reserved for things that actually
mean good or bad, so colouring them would make a normal backlog read as a
failure.


## Editing it

The board is provisioned from `grafana/dashboards/fleet.json` and is read-only in
the UI on purpose — edits there would be silently reverted. Change the JSON and
Grafana reloads it within 30 seconds. To iterate visually instead: copy the
panel, tweak it in the UI, then *Panel JSON* → paste back into the file.

Colours follow a validated palette: sequential blue where the number is a
*magnitude* (releases behind), and the reserved status colours only where a
colour actually means good or bad. Status cells always carry a glyph and a word
as well, so nothing depends on colour alone.


## Traps worth not re-introducing

JSON cannot carry comments, so these are recorded here instead. The first two
were live in the first cut of this board.

**Never write `count(...) or vector(0)` in a panel with a time range.** The
fallback fires at *every* timestamp where the left side has no series, so the
chart paints a confident flat zero across all the time before the collector
existed — it asserts "nothing was wrong last week" about a week it knows nothing
about. Use the `bool` modifier instead: `sum(jq_ci_last_run_success == bool 0)`
evaluates per existing series, so it is 0 when the repos really are all green
and simply absent when there is no data. `or vector(0)` is only safe on a stat
tile reading a single instant.

**Renaming a label starts a new series.** The `repo` label changed from a bare
name to `owner/name` when the cvxgrp repos were added, so history from before
that point lives under the old names. Windows spanning the change show both
generations; they age out. This is inherent to Prometheus, not a misconfiguration.

**Never `group_left` onto an `_info` metric without a `topk` guard.** The
`_info` metrics carry a label that changes value — `ref`, `branch`,
`conclusion`, `checks`. When one changes, Prometheus returns the old *and* the
new series until the old is marked stale, and `group_left` then aborts the whole
query with `found duplicate series for the match group`. The panel goes blank
exactly while someone is doing the work the board exists to show — which is how
this was found, mid-`rhiza` bump. Wrap the right-hand side:
`group_left(ref) topk by (repo) (1, jq_rhiza_template_ref_info)`. During the
overlap it may show the older label value; that self-corrects on the next
scrape, which is far better than an error. The same applies to `and on(...)`,
which does not error but does double-count.

**A `state-timeline` colours from thresholds, not from value mappings.** With
`color.mode: thresholds` and no `thresholds` block, Grafana silently falls back
to its default steps (green up to 80), so `0` and `1` both render green and a
failing repo looks fine. Define the steps explicitly and let the mappings supply
only the words.

## Alerting

Six rules are provisioned into the `Jebel-Quant` folder, each multi-dimensional
so you get one alert instance per repo:

| Rule | Fires when |
|---|---|
| CI red on the default branch | last completed run failed, for 24h |
| Repo behind the template | one or more releases behind, for 7d |
| Open pull request with red checks | a PR's checks are red, for 24h |
| Working copy left dirty | uncommitted changes, for 3d |
| Clone parked off its default branch | non-default branch checked out, for 3d |
| Collector is not refreshing | no successful refresh in 30m |

The long `for` windows are deliberate: this is a fleet board, not a pager, and a
repo that goes red for an hour during a normal push-fix cycle is not worth a
notification.

**Notifications need one more step.** The rules fire and show up in the Grafana
UI as provisioned, but they route to Grafana's built-in email contact point,
which has no SMTP configured — so nothing is delivered anywhere yet. To actually
get notified, add a contact point (Slack webhook, email, ntfy) under
*Alerting → Contact points* and point the default notification policy at it.
Provisioning it here would mean committing a webhook URL to the repo.

