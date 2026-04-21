#!/bin/bash
set -e

# Creates directories for persisted data if they don't exist
mkdir -p /app/data
mkdir -p /app/profiles

# Supervisor orchestrates all 4 processes: Xvfb, x11vnc, noVNC, app
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/app.conf
