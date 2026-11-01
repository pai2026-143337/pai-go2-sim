"""Accept Japanese commands without blocking emergency stop input.

Usage: .venv/bin/python -m llm.client [--provider ollama --model qwen3:8b]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

from llm.config import ClientConfig
from llm.diagnostics import record_failure, redact
from llm.mcp_bridge import Bridge
from llm.safety import SafetyError
from llm.types import RunResult

STOP_WORDS = {'停止', '止まって', 'ストップ', 'stop'}


async def run_instruction(text: str, config: ClientConfig, *, backend=None, stop_event=None) -> RunResult:
    started = time.monotonic()
    input_wall_ns = time.time_ns()
    bridge = Bridge(started)
    response_s = None
    status, reason, explanation = 'failed', None, ''
    failures = []
    observed_error = None

    def remember(step, exc, *, backend_error=False):
        # MCP/backend errors are already logged at the precise failing step.
        details = getattr(bridge, 'failure_details', ()) if backend_error else ()
        failures.extend(details or (record_failure(step, exc, api_key=config.api_key),))

    def first_response():
        nonlocal response_s
        if response_s is None:
            response_s = time.monotonic() - started

    task = waiter = None
    step = 'backend.initialize'
    try:
        if backend is None:
            from llm.sdk_backend import SDKBackend
            backend = SDKBackend()
        step = 'backend.run'
        if text.strip() in STOP_WORDS or (stop_event is not None and stop_event.is_set()):
            status, reason = 'cancelled', 'explicit_stop'
            remember('instruction.input', asyncio.CancelledError('explicit_stop'))
        elif not text.strip():
            status, reason, explanation = 'clarification', 'ambiguous', '指示を入力してください。'
        else:
            task = asyncio.create_task(backend.run(text, config, bridge, first_response))
            waiter = asyncio.create_task(stop_event.wait()) if stop_event is not None else None
            watched = {task, waiter} if waiter else {task}
            # Reserve the last six seconds for bounded stop processing.
            done, _ = await asyncio.wait(watched, timeout=max(.001, config.total_timeout_s - min(6, config.total_timeout_s * .05)), return_when=asyncio.FIRST_COMPLETED)
            if waiter and waiter in done:
                status, reason = 'cancelled', 'explicit_stop'
                remember('instruction.wait', asyncio.CancelledError('explicit_stop'))
            elif task not in done:
                status, reason = 'timeout', 'total_timeout'
                remember('instruction.wait', TimeoutError('Total instruction timeout exceeded'))
            else:
                explanation = task.result()
                status = 'completed' if bridge.motion_start_s is not None else 'clarification'
                reason = None if status == 'completed' else 'ambiguous'
    except asyncio.CancelledError as exc:
        observed_error = exc
        status, reason = 'cancelled', 'user_interrupt'
        remember(getattr(bridge, 'step', step) if step != 'backend.initialize' else step, exc, backend_error=True)
    except TimeoutError as exc:
        observed_error = exc
        status, reason = 'timeout', 'llm_timeout'
        remember(getattr(bridge, 'step', step) if step != 'backend.initialize' else step, exc, backend_error=True)
    except SafetyError as exc:
        observed_error = exc
        reason = exc.reason
        status = 'rejected' if reason == 'argument_error' else 'failed'
        remember(getattr(bridge, 'step', step) if step != 'backend.initialize' else step, exc, backend_error=True)
    except Exception as exc:
        observed_error = exc
        status, reason = 'failed', 'communication_failure'
        remember(getattr(bridge, 'step', step) if step != 'backend.initialize' else step, exc, backend_error=True)
    finally:
        for t in (task, waiter):
            if t is not None and not t.done():
                t.cancel()
        outcomes = await asyncio.gather(*(t for t in (task, waiter) if t is not None), return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError) and outcome is not observed_error:
                remember('backend.cancel', outcome)
        try:
            stopped = await asyncio.wait_for(backend.stop(config), 6) if backend is not None else True
            stop_details = getattr(backend, 'stop_failure_details', ())
            failures.extend(stop_details)
            if not stopped and not stop_details:
                remember('backend.stop', RuntimeError('Stop command was not confirmed'))
        except (Exception, asyncio.CancelledError) as exc:
            stopped = False
            stop_details = getattr(backend, 'stop_failure_details', ())
            if stop_details:
                failures.extend(stop_details)
            else:
                remember('backend.stop', exc)
        if not stopped and status == 'completed':
            status, reason = 'failed', 'stop_unconfirmed'
    if status not in ('completed', 'clarification'):
        explanation = {'cancelled': '停止しました。', 'timeout': '制限時間を超えたため中止しました。',
                       'rejected': '許可範囲外の引数または送信失敗のため中止しました。'}.get(status, '接続に失敗したため中止しました。')
    explanation = redact(explanation, api_key=config.api_key)
    detail = ({**failures[0], **({'secondary_failures': failures[1:]} if len(failures) > 1 else {})}
              if failures else None)
    llm_usage = tuple(getattr(backend, 'llm_usage', ()))
    totals = [usage.get('total_tokens') for usage in llm_usage]
    total_tokens = sum(totals) if totals and all(type(total) is int and total >= 0 for total in totals) else None
    return RunResult(status, explanation, reason, response_s, bridge.motion_start_s, time.monotonic() - started,
                     bridge.calls, input_wall_ns=input_wall_ns, failure_detail=detail,
                     llm_usage=llm_usage, total_tokens=total_tokens)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--provider', choices=('openai', 'ollama'))
    p.add_argument('--base-url')
    p.add_argument('--model')
    p.add_argument('--mcp-command')
    p.add_argument('--text')
    p.add_argument('--instruction-id')
    p.add_argument('--output', type=Path)
    return p


def save_result(path, result):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'result.json').write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + '\n')
    (path / 'tool_calls.jsonl').write_text(''.join(json.dumps(c, ensure_ascii=False) + '\n' for c in result.tool_calls))


async def console(args, config):
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    stop = asyncio.Event()
    loop.add_signal_handler(signal.SIGINT, stop.set)
    loop.add_signal_handler(signal.SIGTERM, stop.set)

    def read():
        line = sys.stdin.readline()
        if not line:
            loop.remove_reader(sys.stdin.fileno())
            queue.put_nowait(None)
        elif line.strip() in STOP_WORDS:
            stop.set()
        else:
            queue.put_nowait(line.strip())

    try:
        loop.add_reader(sys.stdin.fileno(), read)
    except (OSError, PermissionError):
        pass
    text = args.text
    if args.instruction_id:
        from llm.recording import load_instructions
        text = next(i['text'] for i in load_instructions()['instructions'] if i['id'] == args.instruction_id)
    last = None
    try:
        while True:
            if text is None:
                print('指示を入力（動作中は「停止」または Ctrl-C）: ', flush=True)
                incoming = asyncio.create_task(queue.get())
                stopped = asyncio.create_task(stop.wait())
                done, _ = await asyncio.wait({incoming, stopped}, return_when=asyncio.FIRST_COMPLETED)
                if stopped in done:
                    text = '停止'
                    if incoming in done:
                        queue.put_nowait(incoming.result())
                else:
                    text = incoming.result()
                for t in (incoming, stopped):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(incoming, stopped, return_exceptions=True)
                if text is None:
                    break
            last = await run_instruction(text, config, stop_event=stop)
            print(json.dumps(last.to_dict(), ensure_ascii=False), flush=True)
            if args.output:
                save_result(args.output, last)
            if args.text is not None or args.instruction_id is not None:
                break
            text = None
            stop.clear()
    finally:
        try:
            loop.remove_reader(sys.stdin.fileno())
        except (OSError, ValueError):
            pass
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
    return 0 if last and last.status == 'completed' else 1


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = ClientConfig.from_env(provider=args.provider, model=args.model, base_url=args.base_url, mcp_command=args.mcp_command)
    except ValueError as exc:
        detail = record_failure('config.from_env', exc, api_key=os.environ.get('OPENAI_API_KEY', ''))
        if args.output:
            save_result(args.output, RunResult('failed', '設定を確認してください。', 'configuration_error',
                                              None, None, 0, failure_detail=detail))
        return 2
    try:
        return asyncio.run(console(args, config))
    except (Exception, asyncio.CancelledError) as exc:
        detail = record_failure('console.run', exc, api_key=config.api_key)
        if args.output:
            save_result(args.output, RunResult('failed', '処理に失敗しました。', 'communication_failure',
                                              None, None, 0, failure_detail=detail))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
