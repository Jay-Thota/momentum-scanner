#!/bin/bash
echo "=== Scheduler status ==="
launchctl list | grep momentumscanner || echo "NOT RUNNING"
echo ""
echo "=== Last 20 log lines ==="
tail -20 logs/scheduler.log 2>/dev/null || echo "No log file yet"
