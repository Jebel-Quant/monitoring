# One image, one `docker run`. Prometheus, Grafana and the collector in a single
# container, with the dashboard, the datasource, the alert rules and the scrape
# config baked in - so nothing has to be cloned to run the board.
#
# The three used to be two containers plus a launchd agent on the host, because
# the collector reads your working copies and a bind mount could only name a
# checkout at /repos/<owner>/<name>. That constraint is gone: the home
# directory is mounted once, whole, at /host, and repos.yml names paths inside
# it. One mount expresses any layout.
#
#   docker build -t jq-monitoring .

FROM prom/prometheus:v2.55.1 AS prometheus
# The Ubuntu variant, not the default Alpine one: the runtime below is Debian,
# and a musl-linked grafana will not start there.
FROM grafana/grafana:11.3.1-ubuntu AS grafana

FROM python:3.12-slim-bookworm

# ghcr.io reads the package page's description from these, and shows a "add
# LABEL org.opencontainers.image.description" hint until they exist. CI passes
# the same keys as --label from the repo metadata, which wins over these; they
# are here so a plain `docker build` produces a labelled image too.
LABEL org.opencontainers.image.title="Fleet monitoring" \
      org.opencontainers.image.description="Grafana + Prometheus board for a repo fleet: template drift, CI on the default branch, open pull requests, and your local working copies." \
      org.opencontainers.image.source="https://github.com/Jebel-Quant/monitoring" \
      org.opencontainers.image.url="https://jebel-quant.github.io/monitoring/" \
      org.opencontainers.image.licenses="MIT"

# git for the working-copy half, curl for the healthcheck and purge-repo,
# ca-certificates for api.github.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=prometheus /bin/prometheus /bin/promtool /usr/local/bin/
COPY --from=grafana /usr/share/grafana /usr/share/grafana
COPY --from=grafana /etc/grafana /etc/grafana

# The mounted checkouts belong to your host user, not to root inside here, and
# git refuses to read a repository owned by someone else. Nothing here ever
# writes - every call passes --no-optional-locks and the mount is read-only -
# so the ownership check is protecting against nothing we do.
RUN git config --system safe.directory '*'

# Baked in, so there is no second copy on your disk to edit by mistake: the
# only file you own is repos.yml.
COPY prometheus/prometheus.yml /etc/prometheus/prometheus.yml
COPY grafana/provisioning /etc/grafana/provisioning
COPY grafana/dashboards /etc/grafana/dashboards

COPY collector /src/collector
RUN pip install --no-cache-dir /src/collector && rm -rf /src

COPY image/entrypoint.sh /usr/local/bin/entrypoint
COPY image/purge-repo.sh /usr/local/bin/purge-repo
RUN chmod +x /usr/local/bin/entrypoint /usr/local/bin/purge-repo

ENV GF_PATHS_HOME=/usr/share/grafana \
    GF_PATHS_CONFIG=/etc/grafana/grafana.ini \
    GF_PATHS_PROVISIONING=/etc/grafana/provisioning \
    GF_PATHS_DATA=/data/grafana \
    GF_PATHS_LOGS=/data/grafana/log \
    GF_PATHS_PLUGINS=/data/grafana/plugins \
    GF_AUTH_ANONYMOUS_ENABLED=true \
    GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer \
    GF_USERS_DEFAULT_THEME=dark \
    GF_ANALYTICS_REPORTING_ENABLED=false \
    GF_ANALYTICS_CHECK_FOR_UPDATES=false \
    GF_PLUGINS_PREINSTALL_DISABLED=true
# admin/admin is Grafana's own default and is deliberately not restated here:
# the board opens without signing in and the password is only for settings.
# Override with -e GF_SECURITY_ADMIN_PASSWORD=... if you expose port 3000.
ENV JQ_REPOS_FILE=/config/repos.yml \
    JQ_HOST_ROOT=/host \
    PROM_RETENTION=180d

# /config is yours (repos.yml, read-only); /host is your home directory,
# read-only; /data is the history and Grafana's own database.
VOLUME /data
EXPOSE 3000 9090 9109

HEALTHCHECK --interval=30s --start-period=60s --retries=3 \
  CMD curl -fsS http://localhost:3000/api/health >/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint"]
