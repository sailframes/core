#!/bin/bash
# Stop all SailFrames services
echo "Stopping SailFrames services..."
for service in gps imu pressure wind camera monitor; do
    sudo systemctl stop "sailframes-${service}.service"
    echo "  sailframes-${service}: stopped"
done
echo "All services stopped."
