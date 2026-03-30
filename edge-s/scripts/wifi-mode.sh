#!/bin/bash
# SailFrames WiFi Mode Switcher
# Switches between Access Point mode and Client mode

set -e

WIFI_DEVICE="wlan0"
AP_SSID="s1"
AP_PASSWORD="hellowifi"
AP_CONNECTION="sailframes-ap"
CLIENT_SSID="Home-IOT"
CLIENT_CONNECTION="netplan-wlan0-Home-IOT"
MODE_FILE="/etc/sailframes/wifi-mode"

# Ensure mode file directory exists
mkdir -p /etc/sailframes

get_current_mode() {
    # Check which connection is active
    active=$(nmcli -t -f NAME connection show --active | grep -E "^(sailframes-ap|netplan-wlan0)" | head -1)
    if [[ "$active" == "$AP_CONNECTION" ]]; then
        echo "ap"
    else
        echo "client"
    fi
}

get_saved_mode() {
    if [[ -f "$MODE_FILE" ]]; then
        cat "$MODE_FILE"
    else
        echo "ap"  # Default to AP mode
    fi
}

save_mode() {
    echo "$1" > "$MODE_FILE"
}

create_ap_connection() {
    # Check if AP connection already exists
    if nmcli connection show "$AP_CONNECTION" &>/dev/null; then
        echo "AP connection already exists"
        return 0
    fi

    echo "Creating Access Point connection..."
    nmcli connection add \
        type wifi \
        ifname "$WIFI_DEVICE" \
        con-name "$AP_CONNECTION" \
        autoconnect no \
        ssid "$AP_SSID" \
        mode ap \
        ipv4.method shared \
        ipv4.addresses 192.168.4.1/24 \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$AP_PASSWORD"

    echo "AP connection created: SSID=$AP_SSID, Password=$AP_PASSWORD"
}

switch_to_ap() {
    echo "Switching to Access Point mode..."

    # Create AP connection if it doesn't exist
    create_ap_connection

    # Disconnect current connection
    nmcli device disconnect "$WIFI_DEVICE" 2>/dev/null || true
    sleep 1

    # Activate AP mode
    nmcli connection up "$AP_CONNECTION"

    save_mode "ap"
    echo "Access Point mode active: SSID=$AP_SSID, IP=192.168.4.1"
}

switch_to_client() {
    echo "Switching to Client mode..."

    # Disconnect current connection
    nmcli device disconnect "$WIFI_DEVICE" 2>/dev/null || true
    sleep 1

    # Connect to home network
    if nmcli connection show "$CLIENT_CONNECTION" &>/dev/null; then
        nmcli connection up "$CLIENT_CONNECTION"
    else
        # Try connecting by SSID if connection doesn't exist
        nmcli device wifi connect "$CLIENT_SSID"
    fi

    save_mode "client"
    echo "Client mode active: Connected to $CLIENT_SSID"
}

apply_saved_mode() {
    mode=$(get_saved_mode)
    echo "Applying saved WiFi mode: $mode"
    if [[ "$mode" == "ap" ]]; then
        switch_to_ap
    else
        switch_to_client
    fi
}

status() {
    current=$(get_current_mode)
    saved=$(get_saved_mode)

    echo "Current mode: $current"
    echo "Saved mode (boot): $saved"
    echo ""

    if [[ "$current" == "ap" ]]; then
        echo "Access Point: $AP_SSID"
        echo "Password: $AP_PASSWORD"
        echo "IP: 192.168.4.1"
    else
        ip=$(nmcli -g IP4.ADDRESS device show "$WIFI_DEVICE" | cut -d/ -f1)
        echo "Connected to: $CLIENT_SSID"
        echo "IP: $ip"
    fi
}

case "${1:-status}" in
    ap)
        switch_to_ap
        ;;
    client)
        switch_to_client
        ;;
    toggle)
        current=$(get_current_mode)
        if [[ "$current" == "ap" ]]; then
            switch_to_client
        else
            switch_to_ap
        fi
        ;;
    boot)
        apply_saved_mode
        ;;
    status)
        status
        ;;
    current)
        get_current_mode
        ;;
    *)
        echo "Usage: $0 {ap|client|toggle|boot|status|current}"
        echo ""
        echo "  ap      - Switch to Access Point mode (SSID: $AP_SSID)"
        echo "  client  - Switch to Client mode (connect to $CLIENT_SSID)"
        echo "  toggle  - Toggle between modes"
        echo "  boot    - Apply saved mode (for startup)"
        echo "  status  - Show current WiFi status"
        echo "  current - Print current mode (ap/client)"
        exit 1
        ;;
esac
