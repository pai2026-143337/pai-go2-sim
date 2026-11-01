#!/usr/bin/env bash
# Run inside the container: fetch the base repository at a pinned commit, install dependencies, build.
# Safe to run again: an existing clone is only verified, and the build can be repeated.
set -euo pipefail

BASE_URL="https://github.com/rahgirrafi/unitree-go2-ros2.git"
BASE_BRANCH="ignition"
BASE_COMMIT="dfbc03947d"
WS="${WS:-$HOME/ws}"
BASE_DIR="$WS/src/unitree-go2-ros2"

# ROS setup files read unset variables, so relax -u while sourcing them.
set +u
source /opt/ros/humble/setup.bash
set -u

if [ ! -d "$BASE_DIR/.git" ]; then
  git clone --branch "$BASE_BRANCH" "$BASE_URL" "$BASE_DIR"
fi
git -C "$BASE_DIR" checkout --quiet "$BASE_COMMIT"
git -C "$BASE_DIR" submodule update --init --recursive
echo "Base repository commit: $(git -C "$BASE_DIR" rev-parse HEAD)"

sudo apt-get update
sudo apt-get install -y python3-rosdep libignition-msgs8-dev
if [ ! -f /etc/ros/rosdep/sources.list.d/20-default.list ]; then
  sudo rosdep init
fi
rosdep update
# gazebo_plugins belongs to Gazebo Classic, which has no arm64 package and is not used with Fortress.
rosdep install --from-paths "$WS/src" --ignore-src -r -y --skip-keys "gazebo_plugins"

cd "$WS"
colcon build --symlink-install
echo "Build finished. In every terminal run:"
echo "  source /opt/ros/humble/setup.bash && source $WS/install/setup.bash"
