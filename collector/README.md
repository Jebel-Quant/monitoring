# jq-collector

Exports the state of the Jebel-Quant repo fleet as Prometheus metrics on
`:9109/metrics`. Two sources, refreshed on separate cadences: the GitHub REST
API (template drift, CI, pull requests) and the local clones (branch, dirty
files, sync). See `../README.md` for the whole stack.

## Development

Everything below is what CI runs, in the order it runs it, so a green local
pass means a green pull request. The tools come from `uv.lock` on the Python in
`.python-version`; `--frozen` uses the lock as committed rather than
re-resolving it, exactly as CI does.

From `collector/`:

```bash
uv run --frozen ruff check jq_collector tests         # Lint
uv run --frozen ruff format --check jq_collector tests  # Format
uv run --frozen mypy                                   # Types - scope lives in [tool.mypy]
uv run --frozen python -m pytest --cov=jq_collector    # Tests, doctests included
uv run --frozen --python 3.11 python -m pytest         # The requires-python floor
```

The test run fails below 100% coverage of `jq_collector` on its own -
`fail_under` sits in `[tool.coverage.report]`, so there is no flag to forget.
The 3.11 run rebuilds `.venv` on that interpreter; the next plain `uv run`
rebuilds it on 3.12 again, so run the two one after the other, not side by side.

From the repository root:

```bash
python3 scripts/check-dashboard.py                  # Panel ids, link targets, grid overlaps
for f in image/*.sh; do bash -n "$f"; done          # The entry points parse
GITHUB_TOKEN=dummy docker compose config --quiet    # The compose file parses
docker build -t jq-monitoring:ci .                  # The image builds
uvx --with mkdocs-material==9.6.14 mkdocs build --strict  # The book, broken links fail it
```

CI then starts that image against a throwaway `repos.yml` and checks it serves
metrics, and that a malformed `repos.yml` stops the container rather than
running a repo short. `make up` against your own `repos.yml` is the local
equivalent of the first; the exact assertions are in the `image` job of
`.github/workflows/ci.yml`.
