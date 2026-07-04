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
echo "Starting dashboard..."
python -u dashboard/app.py 2>&1
