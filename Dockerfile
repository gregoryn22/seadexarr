FROM python:3.13-slim

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir setuptools && \
    pip install --no-cache-dir -e .

# --- Core paths ---
ENV CONFIG_DIR=/config
ENV DOCKER_ENV=true

# --- Run mode ---
# audit       run audit on a repeating schedule (default)
# audit-once  run audit once and exit
# sync        run existing grab/sync mode on a repeating schedule
ENV RUN_MODE=audit

# --- Schedule (hours) ---
ENV SCHEDULE_TIME=6
ENV AUDIT_SCHEDULE_TIME=6

# --- Audit flags passed to "seadexarr audit" ---
# Options: --apply-tags  --dry-run  --notify-only
# Combine:  --apply-tags   (apply tags, still no downloads)
ENV AUDIT_ARGS=--apply-tags

# --- Logging ---
ENV LOG_LEVEL=INFO

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME ["/config"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
