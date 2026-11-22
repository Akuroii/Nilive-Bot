#!/bin/bash
set -eo pipefail
set -m

echo "Starting Nilive-Bot container..."
cd "${APP_DIR:-/app}"

cleanup_and_exit() {
    local target_exit_code=$1
    echo "[start.sh] Reaping service process groups..."
    kill -TERM -"$BOT_SUPERVISOR_PID" 2>/dev/null || true
    kill -TERM -"$DASHBOARD_PID" 2>/dev/null || true

    for _ in {1..20}; do
        if ! kill -0 "$BOT_SUPERVISOR_PID" 2>/dev/null && ! kill -0 "$DASHBOARD_PID" 2>/dev/null; then
            break
        fi
        sleep 0.1
    done

    kill -KILL -"$BOT_SUPERVISOR_PID" 2>/dev/null || true
    kill -KILL -"$DASHBOARD_PID" 2>/dev/null || true

    wait "$BOT_SUPERVISOR_PID" 2>/dev/null || true
    wait "$DASHBOARD_PID" 2>/dev/null || true

    # Invalidate persisted authorization only now that every service is dead: while the bot
    # was alive it could restore authorization right after the barrier had cleared it.
    echo "[start.sh] Invalidating persisted authorization..."
    if ! python3 -m utils.startup_barrier; then
        echo "[start.sh] WARNING: Failed to invalidate persisted authorization during cleanup."
    fi

    echo "[start.sh] All processes reaped. Container exiting with code $target_exit_code."
    exit "$target_exit_code"
}

shutdown() {
    echo "[start.sh] SIGTERM/SIGINT received. Initiating graceful container shutdown..."
    cleanup_and_exit 0
}
trap shutdown TERM INT

# 1. Initial pre-flight barrier
echo "[start.sh] Running pre-flight startup barrier..."
if ! python3 -m utils.startup_barrier; then
    echo "[start.sh] FATAL: Pre-flight startup barrier failed. Container aborting."
    exit 1
fi

# 2. Bot Supervisor Loop (PGID = BOT_SUPERVISOR_PID)
supervise_bot() {
    while true; do
        echo "[supervisor] Running pre-launch barrier..."
        if ! python3 -m utils.startup_barrier; then
            echo "[supervisor] Pre-launch barrier failed! Exiting supervisor loop with failure."
            exit 1
        fi

        echo "[supervisor] Launching main.py..."
        code=0
        python3 -u main.py || code=$?
        echo "[supervisor] Bot process exited with code $code."

        echo "[supervisor] Running post-exit barrier..."
        if ! python3 -m utils.startup_barrier; then
            echo "[supervisor] Post-exit barrier failed! Exiting supervisor loop with failure."
            exit 1
        fi

        echo "[supervisor] Restarting bot in 5s..."
        sleep 5
    done
}

supervise_bot &
BOT_SUPERVISOR_PID=$!
echo "[start.sh] Bot supervisor started with PGID: $BOT_SUPERVISOR_PID"

# 3. Dashboard Daemon (PGID = DASHBOARD_PID)
echo "[start.sh] Starting dashboard..."
python3 -u dashboard/app.py &
DASHBOARD_PID=$!
echo "[start.sh] Dashboard started with PGID: $DASHBOARD_PID"

# 4. Monitor both critical services
CHILD_STATUS=0
wait -fn "$BOT_SUPERVISOR_PID" "$DASHBOARD_PID" || CHILD_STATUS=$?

echo "[start.sh] Critical service terminated with status $CHILD_STATUS."

if [ "$CHILD_STATUS" -eq 0 ]; then
    echo "[start.sh] Service exited unexpectedly with status 0. Escalating to 1."
    cleanup_and_exit 1
else
    cleanup_and_exit "$CHILD_STATUS"
fi
