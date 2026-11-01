"""Bounded, credential-redacted failure records shared by CLI and MCP paths."""
from __future__ import annotations

import json
import re
import sys

_MAX_MESSAGE = 2000
_MAX_EXCEPTIONS = 12


def redact(text: str, *, api_key: str = '') -> str:
    """Remove credentials before either stderr or recording serialization."""
    if api_key and api_key != 'ollama':
        text = text.replace(api_key, '[redacted]')
    text = re.sub(r'(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+(?::[^\s/@]*)?@', r'\1[redacted]@', text)
    text = re.sub(
        r'''(?ix)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\b["']?\s*[:=]\s*)
        (?:"[^"\r\n]*"|'[^'\r\n]*'|[^\r\n]+)''',
        r'\1[redacted]', text)
    text = re.sub(r'(?i)(\b(?:Bearer|Basic)\s+)[^\s,;"\'<>]+', r'\1[redacted]', text)
    text = re.sub(
        r'''(?ix)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)\b["']?\s*[:=]\s*)
        (?:"[^"\n]*"|'[^'\n]*'|[^\s,;&}\]<>]+)''',
        r'\1[redacted]', text)
    return re.sub(r'\bsk-[A-Za-z0-9_-]{16,}', '[redacted]', text)


def record_failure(step: str, exc: BaseException, *, api_key: str = '') -> dict:
    """Write one safe JSON record, including bounded causal/group exceptions."""
    remaining = _MAX_EXCEPTIONS
    seen: set[int] = set()

    def describe(error: BaseException) -> dict:
        nonlocal remaining
        remaining -= 1
        seen.add(id(error))
        detail = {'exception_type': type(error).__name__,
                  'message': redact(str(error), api_key=api_key)[:_MAX_MESSAGE]}
        cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        if cause is not None and id(cause) not in seen and remaining > 0:
            detail['causes'] = [describe(cause)]
        if isinstance(error, BaseExceptionGroup):
            children = []
            for child in error.exceptions:
                if remaining <= 0:
                    break
                if id(child) not in seen:
                    children.append(describe(child))
            if children:
                detail['exceptions'] = children
        return detail

    detail = {'step': redact(step, api_key=api_key), **describe(exc)}
    print(json.dumps({'failure_detail': detail}, ensure_ascii=False), file=sys.stderr, flush=True)
    return detail
