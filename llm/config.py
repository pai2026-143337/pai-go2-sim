"""Environment-only credentials and reproducible host settings."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
MCP_REVISION = '476591ac058f58cae810cc775e50b51f8d3d757c'


def child_environment(env=None) -> dict[str, str]:
    source = os.environ if env is None else env
    return {k: source[k] for k in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT') if k in source}


@dataclass(frozen=True)
class ClientConfig:
    provider: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    mcp_command: str = str(ROOT / '.venv-mcp/bin/ros-mcp')
    llm_timeout_s: float = 60.0
    total_timeout_s: float = 120.0

    @classmethod
    def from_env(cls, env=None, *, provider=None, model=None, base_url=None, mcp_command=None):
        e = os.environ if env is None else env
        p = provider or e.get('GO2_LLM_PROVIDER', 'openai')
        if p not in ('openai', 'ollama'):
            raise ValueError('接続先は openai または ollama を指定してください。')
        default_url = 'http://127.0.0.1:11434/v1' if p == 'ollama' else 'https://api.openai.com/v1'
        url = base_url or e.get('GO2_LLM_BASE_URL') or e.get('OPENAI_BASE_URL') or default_url
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('接続URLに認証情報やクエリを含めることはできません。')
        if p == 'ollama' and parts.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('Ollama は Mac のループバック接続だけを許可します。')
        name = model if model is not None else e.get('GO2_LLM_MODEL', e.get('OPENAI_MODEL', 'gpt-4.1-mini' if p == 'openai' else 'qwen3:8b'))
        if not name or not name.strip():
            raise ValueError('モデル名を指定してください。')
        key = 'ollama' if p == 'ollama' else e.get('OPENAI_API_KEY', '')
        if not key.strip():
            raise ValueError('OPENAI_API_KEY が未設定です。移動は開始しません。')
        return cls(p, name, url, key, mcp_command or e.get('GO2_MCP_COMMAND', cls.mcp_command))

    def public_conditions(self) -> dict:
        return {'provider': self.provider, 'model': self.model, 'base_url': self.base_url,
                'mcp_revision': MCP_REVISION, 'mcp_timeout_s': 45, 'mcp_retries': 0,
                'llm_timeout_s': self.llm_timeout_s, 'total_timeout_s': self.total_timeout_s,
                'rate_hz': 10, 'max_duration_s': 40, 'max_vx': 0.5, 'max_vy': 0.3, 'max_wz': 0.5}
