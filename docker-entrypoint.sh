#!/bin/sh
# SeaDexArr Docker entrypoint.
# Controlled by RUN_MODE env var:
#   audit       (default) — run audit on a repeating schedule
#   audit-once  — run audit once and exit
#   sync        — run existing grab/sync mode on a repeating schedule

set -e

AUDIT_SCHEDULE_TIME="${AUDIT_SCHEDULE_TIME:-6}"
AUDIT_ARGS="${AUDIT_ARGS:---apply-tags}"

case "${RUN_MODE:-audit}" in

  audit)
    echo "SeaDexArr: audit mode, every ${AUDIT_SCHEDULE_TIME}h (args: ${AUDIT_ARGS})"
    while true; do
      seadexarr audit ${AUDIT_ARGS}
      echo "Audit complete. Next run in ${AUDIT_SCHEDULE_TIME}h."
      sleep $(( AUDIT_SCHEDULE_TIME * 3600 ))
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
