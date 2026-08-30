---
title: What it watches
description: The four subjects the collector reports on, the metrics behind them, and why each is shaped that way.
keywords: prometheus metrics, github actions, template drift, pull requests, git working copy, exporter
---

# What it watches, and why

## How it fits together

```
your clones ──(read-only bind mount, git --no-optional-locks)──┐
                                                               ├─► collector ─► Prometheus ─► Grafana
GitHub REST API ──(repos, runs, PRs, releases)─────────────────┘   :9109         :9090         :3000
```

The collector holds a cached snapshot and refreshes it on two independent
timers — GitHub every 5 minutes, the local clones every 60 seconds — so a scrape
never waits on the API, and an API outage does not blank out the local panels.
Prometheus scrapes every 30s and keeps 180 days.

## The four subjects

**Template drift.** Each repo's `.rhiza/template.yml` `ref` against the newest
published `rhiza` release, as a count of releases missed. Repos pinned to a
branch or a sha show as *not a release* rather than being silently counted as
current.

The pointer is **always read from GitHub's default branch, never from your
clone.** Drift is a property of the repo, and a clone can be arbitrarily stale —
an early version read it from disk to save an API call, which made every
un-pulled checkout look like an out-of-date repo and understated fleet progress
badly. Your clone's own pinned ref is still reported, as the *Checkout ref*
column of the local table, so a stale checkout is visible instead of silently
overwriting the truth.

To keep that correctness free, the pointer read is cached against the default
branch's sha: the file cannot have changed if the branch head has not moved, so
a steady-state refresh spends no extra calls.

**CI on the default branch.** The latest completed run of **every** workflow,
not just the most recent run overall. Pull-request runs are excluded, so this is
the state of `main`, not of someone's branch. A repo is red if *any* workflow is
red; `jq_ci_workflow_success{repo,workflow}` carries the per-workflow detail and
`jq_ci_workflows_failing` counts them.

Two traps live here, and the first cut of this collector fell into both:

- Fetching `per_page=1` returns one run, so a repo whose docs build is failing
  reports green because a *different* workflow ran more recently. `homotopy` sat
  green with `Build and publish PDF` broken; correcting it turned up **11**
  failing workflows fleet-wide, one of them red for three months.
- GitHub orders runs by `created_at`, not by when they finished, so even "the
  newest run" can sort below an older one that took longer or was re-run. Runs
  are compared on `updated_at` instead.

Workflows are keyed by **name**, not `workflow_id`. A renamed or recreated
workflow file leaves two ids sharing one name, and since the metric is labelled
by name, keying on the id exported two series with identical labels — Prometheus
dropped 16 samples per scrape with *"samples with different value but same
timestamp"*.

**Open pull requests and issues.** Per PR: age, author, draft flag, and a
rolled-up check state. Check *runs* are used rather than the combined-status
endpoint, because GitHub Actions does not report as a commit status and every
repo would otherwise show as having no checks.

In the pull-request tables the repo, number and title cells all link out: the
number and title open the PR itself, the repo opens its pull-request list. The
URL is built from the `repo` label and the PR number rather than carrying
GitHub's `html_url` as a label — the label is already `owner/name`, so
`github.com/<repo>/pull/<number>` is exactly the canonical URL.

Issue counts cost no extra API call — they ride the repo listing. GitHub's
`open_issues_count` counts pull requests as issues, so the open PR total is
subtracted back out; that PR total is the unclipped one, so a repo with more
open PRs than `JQ_MAX_PRS_PER_REPO` still reports both numbers correctly.

**Local working copies.** Branch, dirty and untracked counts, stashes, ahead and
behind, and whether the clone sits on its default branch. `~/repos` is scanned
two levels deep, and a clone is kept only if its `origin` belongs to a monitored
org or is named explicitly — so an upstream `numpy` checkout, or the repos under
`~/repos/tschm`, are skipped without any extra configuration.

### The `repo` label is `owner/name`

Not the bare name. Two orgs are in scope and a bare name is only unique within
one owner, so the pair is the identity: the dashboard joins table frames on this
label and builds every GitHub link out of it, and both need it unique on its
own. It also makes alert annotations work unmodified — `{{ $labels.repo }}` is
already the full path.

## Two things worth knowing about the local numbers

*Ahead and behind are as stale as your last fetch.* The collector never fetches
— the mount is read-only and it must never be the reason a repo grows an
`index.lock` mid-rebase. So `jq_local_fetch_age_seconds` is exported next to
them; read the two together. Several clones here had not fetched in weeks.

*`In sync` does not have that problem.* It compares the local default-branch
commit with the sha GitHub reports, which needs no fetch at all. When the two
columns disagree, trust this one.

*Nothing on the local row describes the repo.* `Checkout ref` is what your clone
pins, not what the repo pins — when it differs from the *Template drift* row,
your checkout is behind, not the repo.

