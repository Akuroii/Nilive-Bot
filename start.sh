#!/bin/bash
echo "Starting bot..."
cd /app

# RELIABILITY FIX: previously this backgrounded main.py once, did a
# single 5s liveness check, then moved on to run the dashboard in the
# foreground forever. If the bot crashed hours into uptime, nothing
# here noticed or restarted it — the container itself stayed healthy
# (the dashboard process was still alive), so container-level restart
# policies never triggered either. The only signal was the /health
# dashboard page going stale, discovered only if someone happened to
# look.
#
# This now runs the bot inside a small supervisor loop in the
# background: if main.py exits for any reason, it's relaunched after
# a short backoff, indefinitely, for the lifetime of the container.
supervise_bot() {
    while true; do
        echo "[supervisor] Launching bot..."
        python -u main.py 2>&1
        exit_code=$?
        echo "[supervisor] Bot exited with code $exit_code — restarting in 5s..."
        sleep 5
    done
}

supervise_bot &
BOT_SUPERVISOR_PID=$!
echo "Bot supervisor PID: $BOT_SUPERVISOR_PID"
sleep 5
echo "Bot status check..."
if kill -0 $BOT_SUPERVISOR_PID 2>/dev/null; then
    echo "Bot supervisor is running!"
else
    echo "Bot supervisor failed to start!"
fi

# NOTE: the dashboard no longer depends on this sleep for correctness —
# dashboard/app.py calls init_db() itself on import, so the schema exists
# before any Flask route can run, regardless of how long the bot takes to
# come up (or whether it crashes). The sleep above is just to surface a
# quick bot-crashed log line, not to gate DB readiness.
echo "Starting dashboard..."
python -u dashboard/app.py 2>&1
