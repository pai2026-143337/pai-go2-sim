"""Agents SDK adapter with tracing disabled and redacted failure diagnostics."""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path
import time

from llm.mcp_bridge import Bridge, emergency_zero, managed_server, terminate_publisher
from llm.prompts import SYSTEM_PROMPT


def token_usage(model_response) -> dict:
    """Copy only token counts; SDK zero defaults without provider usage are missing."""
    usage = getattr(model_response, 'usage', None)
    counts = {field: value if type(value := getattr(usage, field, None)) is int and value >= 0 else None
              for field in ('input_tokens', 'output_tokens', 'total_tokens')}
    if getattr(model_response, 'raw_usage', None) is None and not any(counts.values()):
        return {field: None for field in counts}
    return counts


class SDKBackend:
    async def run(self, text, config, bridge, response):
        self.llm_usage = ()
        bridge._redaction_key = config.api_key
        bridge.step = 'sdk.import'
        try:
            return await self._run(text, config, bridge, response)
        except BaseException as exc:
            bridge.capture_failure(bridge.step, exc)
            raise

    async def _run(self, text, config, bridge, response):
        from agents import Agent, ModelSettings, OpenAIChatCompletionsModel, OpenAIResponsesModel, RunConfig, RunHooks, Runner, set_tracing_disabled
        from openai import AsyncOpenAI

        bridge.step = 'sdk.configure'
        set_tracing_disabled(True)

        model_class = OpenAIResponsesModel if config.provider == 'openai' else OpenAIChatCompletionsModel

        class TimedModel(model_class):
            async def get_response(self, *args, **kwargs):
                bridge.step = 'llm.request'
                try:
                    if config.provider == 'ollama':
                        # Chat CompletionsへResponses用の推論設定を渡さない。
                        if kwargs.get('model_settings') is not None:
                            kwargs = {**kwargs, 'model_settings': replace(kwargs['model_settings'], reasoning=None)}
                        elif len(args) >= 3:
                            args = (*args[:2], replace(args[2], reasoning=None), *args[3:])
                    result = await asyncio.wait_for(super().get_response(*args, **kwargs), config.llm_timeout_s)
                except BaseException as exc:
                    bridge.capture_failure(bridge.step, exc)
                    raise
                bridge.step = 'agent.run'
                return result

        backend = self

        class Hooks(RunHooks):
            async def on_llm_end(self, context, agent, model_response):
                backend.llm_usage = (*backend.llm_usage, token_usage(model_response))
                response()

        bridge.step = 'mcp.prepare'
        with tempfile.TemporaryDirectory(prefix='go2-mcp-') as tmp:
            pidfile = Path(tmp) / 'publisher.pid'
            server, _ = managed_server(config, bridge, pidfile)
            bridge.step = 'openai.client'
            async with AsyncOpenAI(api_key=config.api_key, base_url=config.base_url, max_retries=0, timeout=config.llm_timeout_s) as api:
                bridge.step = 'agent.create'
                agent = Agent(name='Go2 language command', instructions=SYSTEM_PROMPT,
                              model=TimedModel(model=config.model, openai_client=api),
                              model_settings=ModelSettings(parallel_tool_calls=False, preserve_raw_usage=True),
                              mcp_servers=[server])
                try:
                    bridge.step = 'mcp.initialize'
                    async with server:
                        try:
                            bridge.step = 'agent.run'
                            result = await Runner.run(agent, text, hooks=Hooks(), max_turns=8,
                                                      run_config=RunConfig(tracing_disabled=True))
                            return str(result.final_output)
                        except BaseException as exc:
                            bridge.capture_failure(bridge.step, exc)
                            raise
                        finally:
                            # Stop the OS process before waiting on SDK context cleanup.
                            bridge.step = 'mcp.cleanup'
                            terminate_publisher(pidfile)
                except BaseException as exc:
                    bridge.capture_failure(bridge.step, exc)
                    raise
                finally:
                    terminate_publisher(pidfile)

    async def stop(self, config):
        bridge = Bridge(time.monotonic())
        self.stop_failure_details = ()
        try:
            with tempfile.TemporaryDirectory(prefix='go2-stop-') as tmp:
                return await emergency_zero(config, Path(tmp) / 'publisher.pid', bridge=bridge)
        finally:
            self.stop_failure_details = bridge.failure_details
