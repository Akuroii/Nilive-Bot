#!/bin/bash
echo "Starting bot..."
python main.py > /proc/1/fd/1 2> /proc/1/fd/2 &
BOT_PID=$!
echo "Bot PID: $BOT_PID"
sleep 3
echo "Starting dashboard..."
python dashboard/app.py
