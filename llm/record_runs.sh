#!/usr/bin/env bash
# Mac entry point: record the fixed language instructions (three times each by default).
# Usage: bash llm/record_runs.sh SESSION [--provider openai|ollama --model NAME ...]
#        bash llm/record_runs.sh SESSION --resume [same provider/model options]
#        bash llm/record_runs.sh SESSION --ids 02,06,10 --trials 1
#        bash llm/record_runs.sh SESSION --controller ai15000
# Requires the orchestrator to stop any previous Gazebo instance first.
set -euo pipefail
cd "$(dirname "$0")/.."
SESSION="${1:?session name required}"; shift
[[ "$SESSION" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo 'Invalid session name.' >&2; exit 2; }
NAME="${CONTAINER_NAME:-pai-go2}"
PY="$PWD/.venv/bin/python"
OUT="$PWD/ws/bags/language/$SESSION"
C_OUT="/home/ubuntu/ws/bags/language/$SESSION"
STATE="/tmp/go2-language/$SESSION"
ROS='source /opt/ros/humble/setup.bash && source /home/ubuntu/ws/install/setup.bash'
OBSERVER='PYTHONPATH="/tmp/go2-language${PYTHONPATH:+:$PYTHONPATH}" /usr/bin/python3 -m llm.ros_observer'
CONTROLLER=ai6800
RESUME=no
BOOT_TIMEOUT_SECONDS="${BOOT_TIMEOUT_SECONDS:-${BOOT_SECONDS:-75}}"
[[ "$BOOT_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || { echo 'Boot timeout must be positive seconds.' >&2; exit 2; }
CLIENT_ARGS=()
INIT_ARGS=()
MCP_ARGS=()
while (($#)); do
  case "$1" in
    --resume) RESUME=yes; shift ;;
    --controller)
      [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 2; }
      [[ "$2" == ai6800 || "$2" == ai15000 ]] || { echo 'Controller must be ai6800 or ai15000.' >&2; exit 2; }
      CONTROLLER="$2"; INIT_ARGS+=(--controller "$2"); shift 2 ;;
    --ids|--trials)
      [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 2; }
      INIT_ARGS+=("$1" "$2"); shift 2 ;;
    --provider|--model|--base-url|--mcp-command)
      [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 2; }
      CLIENT_ARGS+=("$1" "$2"); INIT_ARGS+=("$1" "$2"); shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
if [[ "$RESUME" == yes ]]; then
  "$PY" -m llm.session "$OUT" --resume --validate ${INIT_ARGS[@]+"${INIT_ARGS[@]}"}
  plan="$("$PY" -m llm.session "$OUT" --plan ${INIT_ARGS[@]+"${INIT_ARGS[@]}"})"
  [[ -n "$plan" ]] || { echo "$SESSION: all trials already recorded; nothing to resume."; exit 0; }
  CONTROLLER="$("$PY" -m llm.session "$OUT" --controller-name ${INIT_ARGS[@]+"${INIT_ARGS[@]}"})"
else
  [[ ! -e "$OUT" ]] || { echo "Session already exists; use --resume: $OUT" >&2; exit 1; }
fi
case "$CONTROLLER" in
  ai6800)
    CHECKPOINT=/home/ubuntu/ws/models/go2_flat6800/checkpoint.pt
    EXPECTED_SHA=d358d4a99a327f9c34f8538755655affd50cecf9a194a0ce0d374ba3228df8b0 ;;
  ai15000)
    CHECKPOINT=/home/ubuntu/ws/models/go2_flat15000/checkpoint.pt
    # 変換済みファイルのバイト列を照合する。配布元 raw.pt のSHAとは異なる。
    EXPECTED_SHA="$("$PY" -c 'from llm.write_conditions import checkpoint_sha; sha = checkpoint_sha("ai15000"); print(sha or ""); raise SystemExit(0 if sha else "ai15000 checkpoint missing; rerun setup_rl_in_container.sh.")')" ;;
esac
quote() { printf '%q' "$1"; }
c_exec() { docker exec -u ubuntu "$NAME" bash -lc "$1"; }
ros_exec() { c_exec "$ROS && $1"; }
SIM_PIDFILE= BAG_PIDFILE= BRIDGE_PIDFILE= CURRENT= CLIENT_PID=

start_group() {
  local pidfile="$1" log="$2" command="$3"
  c_exec "setsid bash -lc $(quote "$ROS && exec $command") > $(quote "$log") 2>&1 < /dev/null & echo \$! > $(quote "$pidfile")"
}

stop_group() {
  local pidfile="$1"
  [[ -n "$pidfile" ]] || return 0
  # Only signal a process group that this session created. SIGINT lets rosbag write metadata.
  c_exec "PIDFILE=$(quote "$pidfile"); "'if [[ ! -f "$PIDFILE" ]]; then exit 0; fi
    pid=$(cat "$PIDFILE"); [[ "$pid" =~ ^[0-9]+$ ]] || exit 1
    kill -INT -- "-$pid" 2>/dev/null || true
    for ((i=0; i<100; i++)); do
      kill -0 -- "-$pid" 2>/dev/null || { rm -f "$PIDFILE"; exit 0; }
      sleep .1
    done
    kill -TERM -- "-$pid" 2>/dev/null || true
    sleep 2
    kill -KILL -- "-$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
    exit 1' >/dev/null 2>&1
}

conditions() { "$PY" -m llm.write_conditions "$CURRENT" "$@"; }

simulator_present() {
  c_exec "pgrep -f '[r]os2 launch go2_config|[i]gn gazebo|[r]l_policy_node|[r]os2 bag record' >/dev/null"
}

stop_simulator() {
  if ! stop_group "$SIM_PIDFILE"; then
    conditions --status sim_cleanup_failed --reason owned_process_group_did_not_stop
    CURRENT=
    return 1
  fi
  SIM_PIDFILE=
  if simulator_present; then
    conditions --status sim_cleanup_failed --reason simulator_or_recorder_still_running
    CURRENT=
    return 1
  fi
}

wait_for_boot() {
  local launch_log="$1" controller_log="$2" deadline=$((SECONDS + BOOT_TIMEOUT_SECONDS))
  local controller_seen=no
  BOOT_STATUS=controller_inactive
  BOOT_REASON=controller_activation_timeout
  while ((SECONDS < deadline)); do
    if ros_exec 'timeout 5 ros2 control list_controllers' > "$controller_log" 2>&1 && \
        sed 's/\x1b\[[0-9;]*m//g' "$controller_log" | grep -Eq '^rl_joint_effort_controller .* active'; then
      controller_seen=yes
      if c_exec "grep -q -- 'stand-up complete, policy taking over' $(quote "$launch_log")"; then
        return 0
      fi
    fi
    sleep 1
  done
  if [[ "$controller_seen" == yes ]]; then
    BOOT_STATUS=invalid_start_pose
    BOOT_REASON=stand_up_timeout
  fi
  return 1
}
cleanup() {
  local code=$?
  trap - EXIT INT TERM
  if [[ -n "$CLIENT_PID" ]]; then
    kill -INT "$CLIENT_PID" 2>/dev/null || true
    # The client's bounded cancellation terminates its own MCP publisher and sends zero.
    for ((i=0; i<80; i++)); do
      kill -0 "$CLIENT_PID" 2>/dev/null || break
      sleep .1
    done
    kill -TERM "$CLIENT_PID" 2>/dev/null || true
    wait "$CLIENT_PID" 2>/dev/null || true
  fi
  stop_group "$BAG_PIDFILE" || true
  stop_group "$SIM_PIDFILE" || true
  stop_group "$BRIDGE_PIDFILE" || true
  if [[ -n "$CURRENT" ]]; then
    conditions --status interrupted || true
  fi
  exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Never terminate another workflow's simulator or rosbag recorder.
if simulator_present; then
  echo 'Stop the previous simulator/recorder before running language evaluation.' >&2
  exit 1
fi
actual_sha="$(c_exec "sha256sum $CHECKPOINT" | awk '{print $1}')"
[[ "$actual_sha" == "$EXPECTED_SHA" ]] || {
  echo "$CONTROLLER checkpoint checksum mismatch; rerun setup_rl_in_container.sh." >&2
  exit 1
}
docker exec "$NAME" mkdir -p /tmp/go2-language "$STATE"
docker cp llm "$NAME":/tmp/go2-language/
docker exec "$NAME" chown -R ubuntu:ubuntu /tmp/go2-language
# 起動後に記録を始めても、ラッチされたモデルと固定TFを受信する。
c_exec "printf '/robot_description:\n  durability: transient_local\n  reliability: reliable\n  history: keep_last\n  depth: 1\n/tf_static:\n  durability: transient_local\n  reliability: reliable\n  history: keep_last\n  depth: 10\n' > $(quote "$STATE/qos.yaml")"
if ! ros_exec "$OBSERVER preflight"; then
  echo 'ROS observer preflight failed; session aborted before creating runs. Check both ROS setup files, rclpy/message packages, and /usr/bin/python3 in the container.' >&2
  exit 1
fi
# Reserve planned outputs only after the observer runtime is usable.
if [[ "$RESUME" != yes ]]; then
  "$PY" -m llm.write_conditions "$OUT" --initialize ${INIT_ARGS[@]+"${INIT_ARGS[@]}"}
fi
plan="$("$PY" -m llm.session "$OUT" --plan ${INIT_ARGS[@]+"${INIT_ARGS[@]}"})"
# Resume uses the saved identity even if the caller's default model has changed.
for ((i=0; i<${#CLIENT_ARGS[@]}; i+=2)); do
  [[ "${CLIENT_ARGS[i]}" != --mcp-command ]] || MCP_ARGS+=("--mcp-command" "${CLIENT_ARGS[i+1]}")
done
saved_args="$("$PY" -m llm.session "$OUT" --client-args ${INIT_ARGS[@]+"${INIT_ARGS[@]}"})"
CLIENT_ARGS=()
while IFS= read -r arg; do CLIENT_ARGS+=("$arg"); done <<< "$saved_args"
CLIENT_ARGS+=(${MCP_ARGS[@]+"${MCP_ARGS[@]}"})
ros_exec "$OBSERVER metadata --output $(quote "$C_OUT/container_metadata.json")"
if ! c_exec "pgrep -f '[r]osbridge_websocket' >/dev/null"; then
  BRIDGE_PIDFILE="$STATE/bridge.pid"
  start_group "$BRIDGE_PIDFILE" "$C_OUT/rosbridge.log" 'ros2 launch rosbridge_server rosbridge_websocket_launch.xml port:=9090'
fi

record_one() {
  local id="$1" run="$2" attempt prepared=no status=controller_inactive
  local c_run="$C_OUT/$id/$run"
  "$PY" -m llm.session "$OUT" --begin "$id" "$run" ${INIT_ARGS[@]+"${INIT_ARGS[@]}"}
  CURRENT="$OUT/$id/$run"
  conditions --status preparing --metadata "$OUT/container_metadata.json"
  for attempt in 1 2; do
    if simulator_present; then
      conditions --status sim_cleanup_failed --reason simulator_or_recorder_still_running
      CURRENT=
      return 1
    fi
    local initial="$CURRENT/initial-attempt-$attempt.json"
    local c_initial="$c_run/initial-attempt-$attempt.json"
    SIM_PIDFILE="$STATE/sim-$id-$run-$attempt.pid"
    start_group "$SIM_PIDFILE" "$c_run/launch-$attempt.log" \
      "ros2 launch go2_config gazebo.launch.py controller:=model checkpoint:=$CHECKPOINT headless:=True world_init_x:=-5.0"
    if wait_for_boot "$c_run/launch-$attempt.log" "$CURRENT/controllers-$attempt.log"; then
      if ros_exec "$OBSERVER prepare --timeout 40 --output $(quote "$c_initial")"; then
        cp "$initial" "$CURRENT/initial.json"
        conditions --status prepared --attempt "$attempt" --initial "$initial" --preparation "$initial"
        prepared=yes
        break
      fi
      status=invalid_start_pose
      if [[ ! -s "$initial" ]]; then
        printf '{"status":"invalid_start_pose","failure_reason":"observer_failed","pose":null}\n' > "$initial"
      fi
    else
      status="$BOOT_STATUS"
      printf '{"status":"%s","failure_reason":"%s","pose":null}\n' "$status" "$BOOT_REASON" > "$initial"
    fi
    conditions --status "$status" --attempt "$attempt" --initial "$initial" --preparation "$initial"
    stop_simulator || return 1
  done
  if [[ "$prepared" != yes ]]; then
    echo "$CURRENT status=$status"
    CURRENT=
    return
  fi
  BAG_PIDFILE="$STATE/bag-$id-$run.pid"
  start_group "$BAG_PIDFILE" "$c_run/bag.log" \
    "ros2 bag record --storage sqlite3 --max-cache-size 0 --include-unpublished-topics --qos-profile-overrides-path $(quote "$STATE/qos.yaml") -o $(quote "$c_run/bag") /cmd_vel /odom_ground_truth /clock /joint_states /tf /tf_static /robot_description"
  if ! ros_exec "$OBSERVER bag-ready --timeout 30 --bag $(quote "$c_run/bag") --output $(quote "$c_run/recording_ready.json")"; then
    conditions --status recording_not_started
  else
    conditions --status running
    "$PY" -m llm.client --instruction-id "$id" --output "$CURRENT" "${CLIENT_ARGS[@]}" \
      > "$CURRENT/client.log" 2>&1 < /dev/null &
    CLIENT_PID=$!
    wait "$CLIENT_PID" || true
    CLIENT_PID=
    status=client_result
    # This begins after client shutdown/zero. /clock must advance another simulated second.
    if ros_exec "$OBSERVER tail --timeout 30 --output $(quote "$c_run/final.json")"; then
      conditions --status "$status" --final "$CURRENT/final.json"
    else
      conditions --status clock_or_pose_missing --final "$CURRENT/final.json"
    fi
  fi
  if ! stop_group "$BAG_PIDFILE"; then
    conditions --status bag_close_failed
  fi
  BAG_PIDFILE=
  stop_simulator || return 1
  echo "$CURRENT recorded (see conditions.json)"
  CURRENT=
}

while IFS=$'\t' read -r id run; do
  [[ -z "$id" ]] || record_one "$id" "$run"
done <<< "$plan"
