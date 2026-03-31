#!/bin/bash
# SailFrames Sailing Dashboard Startup
# Rotates 5" display 180° and opens browser in kiosk mode

# Wait for Wayland compositor to be ready
sleep 3

export XDG_RUNTIME_DIR=/run/user/$(id -u)
export WAYLAND_DISPLAY=wayland-0

# Rotate display 180 degrees (Newhaven display is mounted upside down)
wlr-randr --output HDMI-A-1 --transform 180

# Wait for monitor service to start
sleep 2

# Open Chromium in kiosk mode to sailing dashboard
# --ozone-platform=wayland: required for Wayland on Pi 5
# --password-store=basic: bypass GNOME keyring prompt
# --disable-application-cache: prevent stale cached pages
chromium --ozone-platform=wayland --kiosk --password-store=basic \
    --disable-application-cache --noerrdialogs --disable-infobars \
    --no-first-run --disable-session-crashed-bubble --disable-translate \
    http://localhost:8080/sailing &

echo "Sailing dashboard started"
