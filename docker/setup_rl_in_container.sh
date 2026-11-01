#!/usr/bin/env bash
# Run inside the container after setup_in_container.sh:
# install PyTorch (CPU), fetch and verify a Go2 flat-terrain policy trained with Isaac Lab,
# and apply the two-line policy.yaml change that matches its training config.
# Safe to run again: each step skips work that is already done.
set -euo pipefail

TORCH_SPEC="${TORCH_SPEC:-torch==2.14.1}"
TORCH_INDEX="https://download.pytorch.org/whl/cpu"

# Policy used for the final result: Isaac Lab flat task, 6800 iterations,
# published in Zhefan-Xu/isaac-go2-ros2 (ckpts/unitree_go2/flat_model_6800.pt).
POLICY_URL="https://raw.githubusercontent.com/Zhefan-Xu/isaac-go2-ros2/isaacsim-4.5/ckpts/unitree_go2/flat_model_6800.pt"
POLICY_SIZE=983394
POLICY_SHA256="d358d4a99a327f9c34f8538755655affd50cecf9a194a0ce0d374ba3228df8b0"
POLICY_DEST_DIR="go2_flat6800"

# Isaac Lab official flat checkpoint (300 iterations), kept for comparison.
OFFICIAL_URL="https://omniverse-content-production.s3-us-west-2.amazonaws.com/Assets/Isaac/5.1/Isaac/IsaacLab/PretrainedCheckpoints/rsl_rl/Isaac-Velocity-Flat-Unitree-Go2-v0/checkpoint.pt"
OFFICIAL_SIZE=983322
OFFICIAL_SHA256="47ccb2046ec3064257997f879c62d64217b937713c30d827883b2c2f40c50439"

# 公開方策（15,000回）。取得時のリビジョンを固定し、新形式を変換する。
PUBLIC15000_URL="https://huggingface.co/OneSleepyGuy/Isaac-Velocity-Flat-Unitree-Go2-v0-No-Env-Noise/resolve/c037000266d6bc779d6e392a95636c035ffeb9c4/model_15000.pt"
PUBLIC15000_SIZE=984063
PUBLIC15000_SHA256="d33547ce9098b2a0016ff24a77fb9faf8da3027427cd1d038922cd6a7a462a70"
PUBLIC15000_DEST_DIR="go2_flat15000"

WS="${WS:-$HOME/ws}"
BASE_DIR="$WS/src/unitree-go2-ros2"
POLICY_YAML="robots/configs/go2_config/config/rl/policy.yaml"
PATCH_FILE="$(cd "$(dirname "$0")" && pwd)/patches/policy_flat.patch"
CONVERTER="$(cd "$(dirname "$0")" && pwd)/convert_rsl_checkpoint.py"

install_torch() {
  if python3 -c "import torch" 2>/dev/null; then
    echo "PyTorch already installed: $(python3 -c 'import torch; print(torch.__version__)')"
    return
  fi
  python3 -m pip install --user --index-url "$TORCH_INDEX" "$TORCH_SPEC"
  echo "PyTorch installed: $(python3 -c 'import torch; print(torch.__version__)')"
}

# fetch_verified <url> <expected size in bytes> <expected sha256> <destination>
fetch_verified() {
  local url="$1" size="$2" sha="$3" dest="$4"
  if [ -f "$dest" ] && echo "$sha  $dest" | sha256sum --check --status; then
    echo "Checkpoint already present and verified: $dest"
    return
  fi
  mkdir -p "$(dirname "$dest")"
  curl -fsSL -o "$dest.tmp" "$url"
  local got_size
  got_size="$(stat -c %s "$dest.tmp")"
  if [ "$got_size" != "$size" ]; then
    echo "Size mismatch: expected $size bytes, got $got_size" >&2
    rm -f "$dest.tmp"
    return 1
  fi
  if ! echo "$sha  $dest.tmp" | sha256sum --check --status; then
    echo "SHA-256 mismatch for $url" >&2
    rm -f "$dest.tmp"
    return 1
  fi
  mv "$dest.tmp" "$dest"
  echo "Checkpoint fetched and verified: $dest"
}

apply_policy_patch() {
  if git -C "$BASE_DIR" apply --reverse --check "$PATCH_FILE" 2>/dev/null; then
    echo "policy.yaml change already applied"
    return
  fi
  git -C "$BASE_DIR" apply "$PATCH_FILE"
  echo "Applied $PATCH_FILE"
  git -C "$BASE_DIR" diff --stat -- "$POLICY_YAML"
}

prepare_public15000() {
  local dir="$WS/models/$PUBLIC15000_DEST_DIR"
  fetch_verified "$PUBLIC15000_URL" "$PUBLIC15000_SIZE" "$PUBLIC15000_SHA256" "$dir/raw.pt"
  # 元の重みと一致する変換済みファイルがあれば、書き直さず終了する。
  python3 "$CONVERTER" "$dir/raw.pt" "$dir/checkpoint.pt"
}

install_torch
fetch_verified "$POLICY_URL" "$POLICY_SIZE" "$POLICY_SHA256" "$WS/models/$POLICY_DEST_DIR/checkpoint.pt"
fetch_verified "$OFFICIAL_URL" "$OFFICIAL_SIZE" "$OFFICIAL_SHA256" "$WS/models/go2_flat/checkpoint.pt"
prepare_public15000
apply_policy_patch
echo "Launch with:"
echo "  ros2 launch go2_config gazebo.launch.py controller:=model checkpoint:=$WS/models/$POLICY_DEST_DIR/checkpoint.pt"
echo "公開方策（15,000回）:"
echo "  ros2 launch go2_config gazebo.launch.py controller:=model checkpoint:=$WS/models/$PUBLIC15000_DEST_DIR/checkpoint.pt"
