#!/usr/bin/env bash
# Create (first time) or start the ROS 2 Humble + noVNC container for the Go2 simulation.
# Open http://127.0.0.1:6080 in a browser after it starts.
set -euo pipefail

IMAGE="${IMAGE:-tiryoh/ros2-desktop-vnc:humble-20261025T0907}"
NAME="${CONTAINER_NAME:-pai-go2}"
PORT="${VNC_PORT:-6080}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker and run this again." >&2
  exit 1
fi

if docker container inspect "$NAME" >/dev/null 2>&1; then
  binding="$(docker inspect --format '{{json (index .HostConfig.PortBindings "9090/tcp")}}' "$NAME")"
  if [[ "$binding" != '[{"HostIp":"127.0.0.1","HostPort":"9090"}]' ]]; then
    echo "Container '$NAME' lacks the required 127.0.0.1:9090 binding. See docs/language_command.md for backup/recreation; this script will not delete it." >&2
    exit 1
  fi
  docker start "$NAME" >/dev/null
  echo "Started existing container '$NAME'."
else
  docker run -d \
    --name "$NAME" \
    -p "127.0.0.1:${PORT}:80" \
    -p "127.0.0.1:9090:9090" \
    --shm-size=1g \
    --security-opt seccomp=unconfined \
    -v "${REPO_DIR}/ws:/home/ubuntu/ws" \
    -v "${REPO_DIR}/docker:/home/ubuntu/docker:ro" \
    "$IMAGE" >/dev/null
  echo "Created container '$NAME' from $IMAGE."
fi

echo "Open http://127.0.0.1:${PORT} in your browser."
