#!/usr/bin/env bash
# ホストのSDKと固定revisionのROS MCPを、共通のMCP版で導入する。
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
python3 -m venv .venv-mcp
.venv-mcp/bin/python -m pip install -r llm/mcp-requirements.txt 'fastmcp==2.14.7' 'ros-mcp @ git+https://github.com/robotmcp/ros-mcp-server.git@476591ac058f58cae810cc775e50b51f8d3d757c'
.venv/bin/python -m pip install -r llm/requirements.txt
.venv-mcp/bin/python -m pip check
.venv/bin/python -m pip check
