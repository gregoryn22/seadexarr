#!/bin/sh
# SeaDexArr Docker entrypoint.
# Controlled by RUN_MODE env var:
#   audit       (default) — run audit on a repeating schedule
#   audit-once  — run audit once and exit
#   sync        — run existing grab/sync mode on a repeating schedule
#
# SIGTERM/SIGINT during an audit run is forwarded to the Python process,
# which finishes the current series then saves state and exits cleanly.
# SIGTERM during the inter-run sleep causes an immediate clean exit.

set -e

AUDIT_SCHEDULE_TIME="${AUDIT_SCHEDULE_TIME:-6}"
AUDIT_ARGS="${AUDIT_ARGS:---apply-tags}"

case "${RUN_MODE:-audit}" in

  audit)
    echo "SeaDexArr: audit mode, every ${AUDIT_SCHEDULE_TIME}h (args: ${AUDIT_ARGS})"
    _child_pid=""
    # Forward SIGTERM/SIGINT to the child process (Python or sleep) so that
    # Python's graceful-shutdown handler fires during an audit run, and the
    # inter-run sleep is interrupted immediately on stop.
    _forward_term() {
      if [ -n "$_child_pid" ]; then
        kill -TERM "$_child_pid" 2>/dev/null
        wait "$_child_pid" 2>/dev/null || true
      fi
      exit 0
    }
    trap '_forward_term' TERM INT
    while true; do
      seadexarr audit ${AUDIT_ARGS} &
      _child_pid=$!
      wait $_child_pid || true
      _child_pid=""
      echo "Audit complete. Next run in ${AUDIT_SCHEDULE_TIME}h."
      sleep $(( AUDIT_SCHEDULE_TIME * 3600 )) &
      _child_pid=$!
      wait $_child_pid || true
      _child_pid=""
    done
    ;;

  audit-once)
    echo "SeaDexArr: audit-once mode (args: ${AUDIT_ARGS})"
    exec seadexarr audit ${AUDIT_ARGS}
    ;;

  sync)
    echo "SeaDexArr: sync mode, every ${SCHEDULE_TIME:-6}h"
    exec seadexarr "$@"
    ;;

  *)
    echo "SeaDexArr: unknown RUN_MODE='${RUN_MODE}', falling back to audit"
    exec seadexarr audit ${AUDIT_ARGS}
    ;;

esac
