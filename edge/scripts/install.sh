#!/bin/bash
# SailFrames Installation Script
# Run on a fresh Raspberry Pi OS (64-bit, Bookworm) installation
# Usage: sudo bash scripts/install.sh

set -e

echo "==============================="
echo "  SailFrames Installer v1.0"
echo "==============================="
echo ""

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo bash scripts/install.sh"
    exit 1
fi

SAILFRAMES_USER=${SUDO_USER:-pi}
SAILFRAMES_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="/etc/sailframes"
DATA_DIR="/mnt/sailframes-data"

echo "[1/8] Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq

echo "[2/8] Installing system dependencies..."
apt-get install -y -qq \
    python3-pip \
    python3-venv \
    python3-dev \
    python3-smbus \
    i2c-tools \
    gpsd \
    gpsd-clients \
    libcamera-dev \
    python3-libcamera \
    python3-picamera2 \
    ffmpeg \
    bluetooth \
    bluez \
    git

echo "[3/8] Enabling hardware interfaces..."
# Enable I2C
raspi-config nonint do_i2c 0

# Enable camera
raspi-config nonint do_camera 0

# Enable serial (for GPS if using UART instead of USB)
raspi-config nonint do_serial_hw 0
raspi-config nonint do_serial_cons 1  # Disable serial console

# Set I2C speed to 400kHz for BNO085
if ! grep -q "i2c_arm_baudrate" /boot/firmware/config.txt 2>/dev/null; then
    echo "dtparam=i2c_arm_baudrate=400000" >> /boot/firmware/config.txt
fi

# Ensure I2C is enabled
if ! grep -q "dtparam=i2c_arm=on" /boot/firmware/config.txt 2>/dev/null; then
    echo "dtparam=i2c_arm=on" >> /boot/firmware/config.txt
fi

echo "[4/8] Installing Python dependencies..."
pip3 install --break-system-packages -r "$SAILFRAMES_DIR/requirements.txt"

echo "[5/8] Setting up configuration..."
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/sailframes.yaml" ]; then
    cp "$SAILFRAMES_DIR/config/sailframes.yaml" "$CONFIG_DIR/sailframes.yaml"
    echo "  Config copied to $CONFIG_DIR/sailframes.yaml"
    echo "  Edit this file to set your device ID, BLE MAC address, etc."
else
    echo "  Config already exists at $CONFIG_DIR/sailframes.yaml, skipping"
fi

echo "[6/8] Setting up data storage..."
mkdir -p "$DATA_DIR"

# Try to mount USB SSD if available
if lsblk | grep -q "sda"; then
    SSD_DEV="/dev/sda1"
    if ! mount | grep -q "$DATA_DIR"; then
        # Format if needed (only if empty/new)
        if ! blkid "$SSD_DEV" 2>/dev/null | grep -q "ext4"; then
            echo "  Formatting $SSD_DEV as ext4..."
            mkfs.ext4 -F "$SSD_DEV"
        fi
        mount "$SSD_DEV" "$DATA_DIR"

        # Add to fstab for auto-mount
        if ! grep -q "sailframes-data" /etc/fstab; then
            echo "$SSD_DEV $DATA_DIR ext4 defaults,nofail 0 2" >> /etc/fstab
        fi
        echo "  USB SSD mounted at $DATA_DIR"
    fi
else
    echo "  WARNING: No USB SSD detected. Data will be stored on SD card at $DATA_DIR"
    echo "  Plug in a USB SSD for reliable video recording."
fi

chown -R "$SAILFRAMES_USER:$SAILFRAMES_USER" "$DATA_DIR"

echo "[7/8] Installing systemd services..."
for service in gps imu pressure wind camera monitor; do
    cat > "/etc/systemd/system/sailframes-${service}.service" << EOF
[Unit]
Description=SailFrames ${service} service
After=network.target bluetooth.target
Wants=bluetooth.target

[Service]
Type=simple
User=${SAILFRAMES_USER}
WorkingDirectory=${SAILFRAMES_DIR}
ExecStart=/usr/bin/python3 ${SAILFRAMES_DIR}/services/sailframes_${service}.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
done

systemctl daemon-reload

# Enable all services to start on boot
for service in gps imu pressure wind camera monitor; do
    systemctl enable "sailframes-${service}.service"
done

echo "[8/8] Adding user to required groups..."
usermod -aG i2c,spi,gpio,video,bluetooth "$SAILFRAMES_USER"

echo ""
echo "==============================="
echo "  SailFrames Installation Complete"
echo "==============================="
echo ""
echo "Next steps:"
echo "  1. Edit config: sudo nano /etc/sailframes/sailframes.yaml"
echo "     - Set device.id (sailframes-01 through sailframes-06)"
echo "     - Set wind.ble_mac_address after first scan"
echo "  2. Reboot for hardware changes: sudo reboot"
echo "  3. Test sensors:"
echo "     python3 tests/test_gps.py"
echo "     python3 tests/test_imu.py"
echo "     python3 tests/test_pressure.py"
echo "     python3 tests/test_wind.py"
echo "     python3 tests/test_camera.py"
echo "  4. Start all services: sudo bash scripts/start.sh"
echo "  5. Check dashboard: http://$(hostname -I | awk '{print $1}'):8080"
echo ""
