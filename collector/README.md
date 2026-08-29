# jq-collector

Exports the state of the Jebel-Quant repo fleet as Prometheus metrics on
`:9109/metrics`. Two sources, refreshed on separate cadences: the GitHub REST
API (template drift, CI, pull requests) and the local clones (branch, dirty
files, sync). See `../README.md` for the whole stack.
