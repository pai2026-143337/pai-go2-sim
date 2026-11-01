#!/usr/bin/env bash
# Run inside pai-go2 after the existing setup scripts. Safe to rerun.
set -euo pipefail
sudo apt-get update
sudo apt-get install -y ros-humble-rosbridge-suite
dpkg-query -W -f='${Package} ${Version}\n' ros-humble-rosbridge-suite
echo 'Start after sourcing ROS: ros2 launch rosbridge_server rosbridge_websocket_launch.xml address:=0.0.0.0 port:=9090'
