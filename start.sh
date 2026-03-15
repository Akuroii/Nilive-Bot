#!/bin/bash
echo "Starting bot..."
python main.py &
BOT_PID=$!
echo "Bot PID: $BOT_PID"
sleep 3
echo "Starting dashboard..."
python dashboard/app.py
