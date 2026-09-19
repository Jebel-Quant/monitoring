# Starting the board is a docker run and stopping it is a docker rm -f. The
# README spells both out and neither needs a Makefile; this file is for the
# machine where you type them often enough to mistype them, and for the two
# things the one-liners leave you to do by hand - waiting out the collector's
# first pass over the fleet, and finding the URL again afterwards.
#
# Every target goes through docker compose, so docker-compose.yml stays the one
# place the mounts, ports and environment live. A recipe that spelled out -v and
# -e a second time would be a second copy to keep in step, and copies drift.

COMPOSE   := docker compose
CONTAINER := jq-fleet
METRICS   := http://127.0.0.1:9109/metrics
BOARD     := http://localhost:3000/d/jq-fleet

# compose reads .env for the token. Where neither the environment nor .env has
# one, borrow gh's - the README asks for that token by name, and the 60 calls an
# hour GitHub allows an anonymous caller is not a fleet. A token already set in
# either place wins, so this never quietly overrides a deliberate one.
ifndef GITHUB_TOKEN
ifeq ($(shell grep -qs '^GITHUB_TOKEN=..' .env && echo set),)
export GITHUB_TOKEN := $(shell gh auth token 2>/dev/null)
endif
endif

.DEFAULT_GOAL := help
.PHONY: help preflight up down restart wait status open logs build pull purge clean

help:  ## List these targets
	@awk 'BEGIN{FS=":.*## "} /^[a-z][a-z-]*:.*## /{printf "  make %-8s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# Three ways the start goes wrong before a single container moves, each with a
# better message from here than from the thing that would have failed. The third
# is the one worth spelling out: a jq-fleet started by hand carries no compose
# labels, so compose will not adopt it - it stops at "container name already in
# use" and leaves you guessing. Removing it loses nothing, the history being in
# the volume and not the container, but that is the kind of thing to be told
# rather than to have done for you.
preflight:
	@docker info >/dev/null 2>&1 \
	  || { echo "Docker is not running - start Docker Desktop, then try again."; exit 1; }
	@test -f repos.yml \
	  || { echo "No repos.yml - copy repos.example.yml and name your fleet."; exit 1; }
	@! docker container inspect $(CONTAINER) >/dev/null 2>&1 \
	  || test -n "$$(docker container inspect $(CONTAINER) \
	       --format '{{index .Config.Labels "com.docker.compose.project"}}')" \
	  || { echo "$(CONTAINER) is already running, started by hand rather than by compose,"; \
	       echo "so compose cannot take it over. 'docker rm -f $(CONTAINER)' first - the"; \
	       echo "history lives in the jq-fleet-data volume and survives that."; exit 1; }

up: preflight  ## Start the board, wait out the first pass, open it
	$(COMPOSE) up -d
	@$(MAKE) --no-print-directory wait
	@$(MAKE) --no-print-directory open

down:  ## Stop the board. The history in jq-fleet-data stays
	$(COMPOSE) down

restart:  ## Reread repos.yml. An added or dropped repo needs this
	docker restart $(CONTAINER)

# The container is healthy as soon as Grafana answers, but the collector holds
# :9109 shut until it has seeded both sources - a minute or two on a fleet this
# size, during which every panel says No data. Waiting on the port rather than
# on health is the difference between "it is up" and "it has something to show".
# The loop watches for the container dying too: a board that crashed and one
# that is still thinking both look like silence from here.
wait:  ## Block until the collector serves its first snapshot
	@echo "Waiting for the collector's first pass over the fleet..."
	@until curl -sf -o /dev/null $(METRICS); do \
	  docker ps --filter name=$(CONTAINER) --filter status=running -q | grep -q . \
	    || { echo "$(CONTAINER) is not running - see 'make logs'"; exit 1; }; \
	  sleep 3; \
	done
	@echo "Serving. $(BOARD)"

status:  ## What is up, and is the collector past its first pass
	@docker ps --filter name=$(CONTAINER) --format 'container  {{.Status}}' | grep . \
	  || echo "container  not running"
	@curl -sf -o /dev/null $(METRICS) \
	  && echo "collector  serving :9109" \
	  || echo "collector  no metrics yet - 'make wait', or 'make logs' if that hangs"
	@curl -sf -o /dev/null http://127.0.0.1:3000/api/health \
	  && echo "grafana    $(BOARD)" \
	  || echo "grafana    not answering"

open:  ## Open the board in a browser
	@open $(BOARD) 2>/dev/null || xdg-open $(BOARD) 2>/dev/null || echo $(BOARD)

logs:  ## Follow all three processes, prefixed
	$(COMPOSE) logs -f

build:  ## Rebuild the image from this working copy
	$(COMPOSE) build

pull:  ## Fetch the published image instead of building one
	$(COMPOSE) pull

purge:  ## Erase a dropped repo's history: make purge REPO=owner/name (irreversible)
	@test -n "$(REPO)" || { echo "Name the repo: make purge REPO=owner/name"; exit 1; }
	docker exec $(CONTAINER) purge-repo $(REPO)

clean:  ## Stop and discard the history as well (irreversible)
	@printf 'Deletes jq-fleet-data - the Prometheus history and Grafana database. Type yes: '; \
	read reply; [ "$$reply" = yes ] || { echo "Left alone."; exit 1; }
	$(COMPOSE) down -v
