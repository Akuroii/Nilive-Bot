#!/bin/bash
echo "Starting bot..."
cd /app
python -u main.py 2>&1 &
BOT_PID=$!
echo "Bot PID: $BOT_PID"
sleep 5
echo "Bot status check..."
if kill -0 $BOT_PID 2>/dev/null; then
    echo "Bot is running!"
else
    echo "Bot crashed! Exit code: $?"
fi

# NOTE: the dashboard no longer depends on this sleep for correctness —
# dashboard/app.py calls init_db() itself on import, so the schema exists
# before any Flask route can run, regardless of how long the bot takes to
# come up (or whether it crashes). The sleep above is just to surface a
# quick bot-crashed log line, not to gate DB readiness.
echo "Starting dashboard..."
python -u dashboard/app.py 2>&1
