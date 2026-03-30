#!/bin/bash
# Display control helper - runs in the user's Wayland session
# Watches /tmp/sailframes-display-control for commands
# Commands: "on" or "off"

CONTROL_FILE="/tmp/sailframes-display-control"

# Create control file if it doesn't exist
touch "$CONTROL_FILE"

echo "Display control helper started, watching $CONTROL_FILE"

# Watch for changes
while true; do
    inotifywait -q -e modify "$CONTROL_FILE" 2>/dev/null || sleep 2

    CMD=$(cat "$CONTROL_FILE" 2>/dev/null)

    case "$CMD" in
        off)
            echo "Turning display OFF"
            wlopm --off HDMI-A-2 2>/dev/null || wlopm --off '*' 2>/dev/null
            ;;
        on)
            echo "Turning display ON"
            wlopm --on HDMI-A-2 2>/dev/null || wlopm --on '*' 2>/dev/null
            ;;
    esac
done
