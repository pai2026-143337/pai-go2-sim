"""MCP allowlist, serial execution, and termination of the actual publisher."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import signal
import sys
import time
from pathlib import Path

from llm.config import ROOT, child_environment
from llm.diagnostics import record_failure
from llm.safety import ALLOWED_TOOLS, SafetyError, check_result, validate_call, velocity, zero_twist


def decode_result(result) -> dict:
    if isinstance(result, dict):
        return result
    if getattr(result, 'isError', False):
        message = '\n'.join(c.text for c in getattr(result, 'content', ()) if c.type == 'text')
        raise SafetyError('MCP の呼出しに失敗しました。', 'communication_failure') from RuntimeError(message or 'MCP isError=True')
    value = getattr(result, 'structuredContent', None)
    if value is None:
        try:
            texts = [c.text for c in result.content if c.type == 'text']
            value = json.loads(texts[0])
        except (AttributeError, IndexError, ValueError) as exc:
            raise SafetyError('MCP の戻り値が不正です。', 'communication_failure') from exc
    if not isinstance(value, dict):
        raise SafetyError('MCP の戻り値が不正です。', 'communication_failure')
    return value


class Bridge:
    def __init__(self, started: float):
        self.started = started
        self.used_s = 0.0
        self.motion_start_s = None
        self.calls = ()
        self.failed = False
        self.lock = asyncio.Lock()
        self.step = 'backend.run'
        self.failure_details = ()
        self._failure_ids = ()
        self._redaction_key = ''

    def capture_failure(self, step, exc):
        """同じ例外は一度だけ、元の失敗ステップで記録する。"""
        if id(exc) not in self._failure_ids:
            self.failure_details += (record_failure(step, exc, api_key=self._redaction_key),)
            self._failure_ids += (id(exc),)

    async def execute(self, name, args, invoke):
        if self.failed or self.lock.locked():
            self.failed = True
            exc = SafetyError('追加移動または同時送信は許可されていません。')
            self.capture_failure(f'mcp.tool.{name}.validate', exc)
            raise exc
        async with self.lock:
            began = time.monotonic() - self.started
            previous_step = self.step
            try:
                self.step = f'mcp.tool.{name}.validate'
                budget = validate_call(name, args, self.used_s)
                self.used_s = budget
                if name == 'publish_for_durations' and any(any(velocity(m)) for m in args['messages']):
                    if self.motion_start_s is None:
                        self.motion_start_s = began
                self.step = f'mcp.tool.{name}.invoke'
                result = await invoke(name, args)
                self.step = f'mcp.tool.{name}.decode'
                value = decode_result(result)
                self.step = f'mcp.tool.{name}.result'
                try:
                    check_result(value, args if name == 'publish_for_durations' else None)
                except SafetyError as exc:
                    error_fields = {k: value[k] for k in ('error', 'errors', 'success', 'published_count') if k in value}
                    raise exc from RuntimeError(json.dumps(error_fields, ensure_ascii=False))
            except BaseException as exc:
                self.capture_failure(self.step, exc)
                self.failed = True
                # Only known-safe validated inputs are persisted; never raw MCP errors.
                self.calls += ({'name': name if name in ALLOWED_TOOLS else 'rejected', 'wall_start_s': began, 'status': 'failed'},)
                raise
            self.calls += ({'name': name, 'arguments': copy.deepcopy(args), 'wall_start_s': began,
                            'wall_end_s': time.monotonic() - self.started, 'status': 'ok',
                            'published_count': value.get('published_count')},)
            self.step = previous_step
            return value


def terminate_publisher(pidfile) -> None:
    """SIGKILL is intentional: ros-mcp's synchronous publishing thread must end."""
    try:
        pid = int(Path(pidfile).read_text())
        os.kill(pid, signal.SIGKILL)
    except (FileNotFoundError, ProcessLookupError):
        pass
    finally:
        Path(pidfile).unlink(missing_ok=True)


def managed_server(config, bridge, pidfile):
    from agents.mcp import MCPServerStdio, create_static_tool_filter
    from agents.tool_guardrails import tool_input_guardrail, ToolGuardrailFunctionOutput
    bridge._redaction_key = config.api_key

    @tool_input_guardrail
    def validate(data):
        try:
            args = json.loads(data.context.tool_arguments)
            validate_call(data.context.tool_name, args, bridge.used_s)
        except Exception as exc:
            bridge.capture_failure(f'mcp.guardrail.{data.context.tool_name}', exc)
            raise
        return ToolGuardrailFunctionOutput.allow()

    class SafeServer(MCPServerStdio):
        async def list_tools(self, *args, **kwargs):
            previous_step = bridge.step
            bridge.step = 'mcp.list_tools'
            try:
                tools = await super().list_tools(*args, **kwargs)
            except BaseException as exc:
                bridge.capture_failure(bridge.step, exc)
                raise
            bridge.step = previous_step
            return tools

        async def call_tool(self, tool_name, arguments, meta=None):
            async def invoke(name, args):
                return await super(SafeServer, self).call_tool(name, args, meta=meta)
            value = await bridge.execute(tool_name, arguments, invoke)
            from mcp.types import CallToolResult, TextContent
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(value))])

    env_args = [f'{k}={v}' for k, v in child_environment().items()]
    # MCP's transport merges default inherited variables; env -i removes them too.
    server = SafeServer(params={'command': '/usr/bin/env',
                        'args': ['-i', *env_args, sys.executable, '-m', 'llm.mcp_process', str(pidfile), config.mcp_command],
                        'cwd': str(ROOT)}, name='go2-ros', cache_tools_list=True,
                        tool_filter=create_static_tool_filter(allowed_tool_names=list(ALLOWED_TOOLS)),
                        tool_input_guardrails=[validate], client_session_timeout_seconds=45,
                        max_retry_attempts=0, failure_error_function=None)
    return server, bridge


async def emergency_zero(config, pidfile, *, bridge=None) -> bool:
    """Fresh bounded connection, after the previous publisher has been killed."""
    b = bridge if bridge is not None else Bridge(time.monotonic())
    b._redaction_key = config.api_key
    b.step = 'stop.mcp.prepare'
    try:
        server, _ = managed_server(config, b, pidfile)

        async def send():
            b.step = 'stop.mcp.initialize'
            async with server:
                await server.call_tool('publish_for_durations', {'topic': '/cmd_vel', 'msg_type': 'geometry_msgs/msg/Twist',
                                       'messages': [zero_twist()], 'durations': [1.0], 'rate_hz': 10})
        await asyncio.wait_for(send(), timeout=6)
        return True
    except asyncio.CancelledError as exc:
        b.capture_failure(b.step, exc)
        raise
    except Exception as exc:
        b.capture_failure(b.step, exc)
        return False
    finally:
        terminate_publisher(pidfile)
