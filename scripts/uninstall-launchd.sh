#!/bin/bash
set -euo pipefail
LABEL="com.automate-jira.sync"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/Library/Application Support/automate-jira"
echo "제거됨: $LABEL"
