#!/usr/bin/env bash
# Record the scripted scenario for one or more gait controllers with rosbag2 (run on the Mac).
# Usage: analysis/record_runs.sh <runs per controller> <controller>... (champ | ai300 | ai6800 | ai15000)
# Each run: restart the sim headless at the scenario start position, record the topics,
# play the scenario on simulation time, stop, and write conditions.json next to the bag.
set -u
cd "$(dirname "$0")/.."
NAME="${CONTAINER_NAME:-pai-go2}"
RUNS="${1:?runs per controller}"; shift
ROS="source /opt/ros/humble/setup.bash && source /home/ubuntu/ws/install/setup.bash"
BASE=/home/ubuntu/ws/src/unitree-go2-ros2
DATE="$(date +%Y%m%d)"
START_X="$(.venv/bin/python -c 'from analysis.scenario import START_X; print(START_X)')"
VERSION="$(.venv/bin/python -c 'from analysis.scenario import SCENARIO_VERSION; print(SCENARIO_VERSION)')"
TOPICS="/cmd_vel /odom_ground_truth /joint_states /foot_contacts /imu/data /rl_joint_effort_controller/commands /joint_group_effort_controller/joint_trajectory /tf /tf_static /robot_description /clock /scenario/segment"

c_exec() { docker exec -u ubuntu "$NAME" bash -lc "$1"; }

checkpoint_for() {
  case "$1" in
    champ) echo "" ;;
    ai300) echo /home/ubuntu/ws/models/go2_flat/checkpoint.pt ;;
    ai6800) echo /home/ubuntu/ws/models/go2_flat6800/checkpoint.pt ;;
    ai15000) echo /home/ubuntu/ws/models/go2_flat15000/checkpoint.pt ;;
    *) echo "unknown controller $1" >&2; return 1 ;;
  esac
}

stop_sim() {
  docker exec "$NAME" bash -c 'pkill -INT -f "ros2 bag record"; pkill -INT -f "ros2 launch go2_config"; sleep 4; pkill -9 -f "ign gazebo"; pkill -9 -f rl_policy_node; pkill -9 -f scenario_driver; true' >/dev/null 2>&1
  sleep 2
}

rtf() { c_exec 'timeout 8 ign topic -e -t /stats -n 1 | grep -o "real_time_factor: [0-9.e-]*" | cut -d" " -f2' 2>/dev/null | head -1; }

prepare_container() {
  docker exec "$NAME" bash -c "mkdir -p /tmp/analysis && chown ubuntu:ubuntu /tmp/analysis"
  docker cp analysis/scenario.py "$NAME":/tmp/analysis/scenario.py
  docker cp analysis/scenario_driver.py "$NAME":/tmp/analysis/scenario_driver.py
  # Latched topics must be recorded with transient-local durability or they are missed.
  c_exec 'printf "/robot_description:\n  durability: transient_local\n  reliability: reliable\n  history: keep_last\n  depth: 1\n/tf_static:\n  durability: transient_local\n  reliability: reliable\n  history: keep_last\n  depth: 10\n" > /tmp/analysis/qos.yaml'
}

record_one() {
  local ctrl="$1" run="$2"
  local ckpt; ckpt="$(checkpoint_for "$ctrl")" || return 1
  local mode=model; [ "$ctrl" = champ ] && mode=champ
  local out="ws/bags/${DATE}_${ctrl}_${run}"
  rm -rf "$out"
  stop_sim
  c_exec 'source /opt/ros/humble/setup.bash && ros2 daemon stop >/dev/null; ros2 daemon start >/dev/null'
  local joint_ctrl=rl_joint_effort_controller; [ "$mode" = champ ] && joint_ctrl=joint_group_effort_controller
  local attempt booted=no
  for attempt in 1 2; do
    docker exec -u ubuntu -d "$NAME" bash -lc "$ROS && ros2 launch go2_config gazebo.launch.py controller:=$mode ${ckpt:+checkpoint:=$ckpt} headless:=True world_init_x:=$START_X > /home/ubuntu/ws/launch_record.log 2>&1"
    sleep "${BOOT_SECONDS:-55}"
    # The base repo loads controllers after a fixed delay; on a slow start they can stay inactive
    # and the robot never moves. Only record once the joint controller is really active.
    if c_exec "$ROS && timeout 15 ros2 control list_controllers" 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | grep -Eq "^$joint_ctrl .* active"; then
      booted=yes; break
    fi
    echo "  $ctrl run $run: $joint_ctrl not active (attempt $attempt), restarting" >&2
    stop_sim
    c_exec 'source /opt/ros/humble/setup.bash && ros2 daemon stop >/dev/null; ros2 daemon start >/dev/null'
  done
  if [ "$booted" != yes ]; then
    mkdir -p "$out"; echo "{\"controller\": \"$ctrl\", \"run\": $run, \"status\": \"controller_inactive\"}" > "$out/conditions.json"
    echo "$out status=controller_inactive"; return 0
  fi
  local rtf_start; rtf_start="$(rtf)"
  docker exec -u ubuntu -d "$NAME" bash -lc "$ROS && ros2 bag record -o /home/ubuntu/$out --qos-profile-overrides-path /tmp/analysis/qos.yaml $TOPICS > /home/ubuntu/ws/bag_record.log 2>&1"
  sleep 3
  local status=ok
  c_exec "$ROS && timeout 80 python3 /tmp/analysis/scenario_driver.py" >/dev/null 2>&1 || status=driver_failed
  local rtf_end; rtf_end="$(rtf)"
  docker exec "$NAME" bash -c 'pkill -INT -f "ros2 bag record"; true'
  sleep 3
  local sha=""; [ -n "$ckpt" ] && sha="$(c_exec "sha256sum $ckpt" | cut -d' ' -f1)"
  local commit; commit="$(c_exec "git -C $BASE rev-parse --short=10 HEAD")"
  stop_sim
  [ -d "$out" ] || status=bag_missing
  mkdir -p "$out"
  cat > "$out/conditions.json" <<JSON
{
  "controller": "$ctrl",
  "run": $run,
  "checkpoint_sha256": "$sha",
  "base_commit": "$commit",
  "recorded_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "scenario_version": $VERSION,
  "start_x": $START_X,
  "rtf_start": "${rtf_start}",
  "rtf_end": "${rtf_end}",
  "status": "$status"
}
JSON
  echo "$out status=$status rtf_start=${rtf_start} rtf_end=${rtf_end}"
}

# 方式を省略した場合は、4方式をすべて記録する。
if [ "$#" -eq 0 ]; then set -- champ ai300 ai6800 ai15000; fi
prepare_container
for ctrl in "$@"; do
  # RUN_IDS="2 3" re-records only those runs (e.g. after a failed run).
  for run in ${RUN_IDS:-$(seq 1 "$RUNS")}; do
    record_one "$ctrl" "$run"
  done
done
