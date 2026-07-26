#!/bin/bash
mkdir -p logs
launchctl load ~/Library/LaunchAgents/com.momentumscanner.plist
echo "Momentum Scanner scheduler started"
launchctl list | grep momentumscanner
