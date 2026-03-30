#!/bin/bash
# Start all SailFrames services
echo "Starting SailFrames services..."
for service in gps imu pressure wind camera monitor; do
    sudo systemctl start "sailframes-${service}.service"
    echo "  sailframes-${service}: $(systemctl is-active sailframes-${service})"
done
echo ""
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):8080"
