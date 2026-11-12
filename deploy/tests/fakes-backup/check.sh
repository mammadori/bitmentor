#!/usr/bin/env bash
# Fake bin/check.sh: the alarm of backup.sh arrives here (--test "<text>")
echo "check $*" >> "$FAKE_CALLS"
