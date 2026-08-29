FROM python:3.12-slim

# git is the collector's other data source, alongside the GitHub API.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The clones are bind-mounted from the host and are owned by a uid this
# container does not have, which git refuses to touch by default. The mount is
# read-only, so widening the trust here cannot lead to a write.
RUN git config --system --add safe.directory '*'

WORKDIR /app
COPY pyproject.toml README.md ./
COPY jq_collector ./jq_collector
RUN pip install --no-cache-dir .

EXPOSE 9109
CMD ["python", "-m", "jq_collector"]
