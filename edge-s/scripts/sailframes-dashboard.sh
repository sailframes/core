#!/bin/bash
# Wait for desktop
sleep 10

# Kill any existing chromium
pkill chromium

# Start first browser (SailFrames)
chromium --user-data-dir=$HOME/.config/chromium-sailframes --password-store=basic --noerrdialogs --disable-infobars http://localhost:8080 &
sleep 5

# Start second browser (Netdata)
chromium --user-data-dir=$HOME/.config/chromium-netdata --password-store=basic --noerrdialogs --disable-infobars 'https://app.netdata.cloud/spaces/avillach-space/rooms/all-nodes/nodes/5c4d691a-7ab0-4812-8eb7-c1fd876e71dc' &
sleep 5

# Get screen dimensions
SCREEN_W=$(xdotool getdisplaygeometry | cut -d' ' -f1)
SCREEN_H=$(xdotool getdisplaygeometry | cut -d' ' -f2)
HALF_W=$((SCREEN_W / 2))

# Tile windows side by side
wmctrl -r 'SailFrames' -e 0,0,0,$HALF_W,$SCREEN_H
wmctrl -r 'Netdata' -e 0,$HALF_W,0,$HALF_W,$SCREEN_H
