#!/bin/bash
#
# Deploy SailFrames to edge device (Raspberry Pi)
#
# Usage:
#   ./deploy-edge.sh [hostname]
#
# Examples:
#   ./deploy-edge.sh s1.local
#   ./deploy-edge.sh 192.168.1.50
#

set -e

HOST="${1:-s1.local}"
USER="paul"
REMOTE_DIR="/home/paul/sailframes"

echo "Deploying to ${USER}@${HOST}..."

# Pull latest from GitHub
echo "Pulling latest from GitHub..."
ssh "${USER}@${HOST}" "cd ${REMOTE_DIR} && git fetch origin && git reset --hard origin/main"

# Show what was deployed
echo ""
echo "Deployed commit:"
ssh "${USER}@${HOST}" "cd ${REMOTE_DIR} && git log --oneline -1"

# Restart services
echo ""
echo "Restarting services..."
ssh "${USER}@${HOST}" "sudo systemctl restart sailframes-battery-logger sailframes-monitor 2>/dev/null || true"

echo ""
echo "Deployment complete."
