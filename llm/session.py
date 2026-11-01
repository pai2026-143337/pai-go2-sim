"""Validate and resume recorded sessions without starting ROS, Docker, or clients."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from llm.config import ClientConfig, ROOT
from llm.recording import load_instructions, planned_pairs, session_pairs
from llm.write_conditions import CHECKPOINT_DIRS, checkpoint_path, checkpoint_sha, trial_conditions, write


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError('セッション記録を読み取れません。') from exc
    if not isinstance(data, dict):
        raise ValueError('セッション記録の形式が不正です。')
    return data


def saved_config(metadata: dict, *, provider=None, model=None, base_url=None, mcp_command=None) -> ClientConfig:
    for key, explicit in (('provider', provider), ('model', model), ('base_url', base_url)):
        value = metadata.get(key)
        if not isinstance(value, str) or any(c in value for c in '\r\n\x00'):
            raise ValueError('保存された接続先・モデルの形式が不正です。')
        if explicit is not None and explicit != value:
            raise ValueError('保存された接続先・モデルと指定が一致しません。')
    if metadata['provider'] not in ('openai', 'ollama'):
        raise ValueError('保存された接続先の形式が不正です。')
    if not metadata['base_url']:
        raise ValueError('保存された接続URLが空です。')
    return ClientConfig.from_env({'OPENAI_API_KEY': 'offline-validation'},
        provider=metadata['provider'], model=metadata['model'], base_url=metadata['base_url'],
        mcp_command=mcp_command)


def validate_session(path: Path, *, resume=False, ids=None, trials=None,
                     controller=None, **flags) -> tuple[ClientConfig, dict]:
    if controller is not None:
        checkpoint_path(controller)
    if not path.exists():
        planned_pairs(ids=ids, trials=trials)
        if resume:
            raise ValueError('--resume のセッションがありません。')
        config = ClientConfig.from_env({**os.environ, 'OPENAI_API_KEY': 'offline-validation'}, **flags)
        # Enforce line-safe arguments before any later shell reconstruction.
        saved_config(config.public_conditions())
        return config, {}
    if not resume:
        raise ValueError('既存セッションには --resume が必要です。')
    metadata = read_json(path / 'session.json')
    saved_controller = metadata.get('controller', 'ai6800')
    checkpoint_path(saved_controller)
    if controller is not None and controller != saved_controller:
        raise ValueError('方策が保存セッションと一致しません。')
    pairs = session_pairs(metadata)
    if ids is not None or trials is not None:
        explicit = planned_pairs(ids=metadata.get('instruction_ids') if ids is None else ids,
                                 trials=metadata.get('trials_per_instruction') if trials is None else trials)
        if explicit != pairs:
            raise ValueError('指示ID・試行回数が保存セッションと一致しません。')
    instructions = load_instructions()
    expected = {'instruction_version': instructions['version'],
                'instruction_set_sha256': hashlib.sha256((ROOT / 'llm/instructions.json').read_bytes()).hexdigest(),
                'checkpoint_sha256': checkpoint_sha(saved_controller), 'planned_runs': len(pairs)}
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise ValueError('問題の版・方策SHA・予定回数が保存セッションと一致しません。')
    config = saved_config(metadata, **flags)
    for instruction, run in pairs:
        condition = path / instruction / str(run) / 'conditions.json'
        if condition.is_file():
            data = read_json(condition)
            if any(key in data and data[key] != metadata.get(key) for key in
                   ('provider', 'model', 'base_url', 'checkpoint_sha256', 'instruction_version')) or (
                    data.get('controller', saved_controller) != saved_controller):
                raise ValueError('試行の保存設定がセッションと一致しません。')
    return config, metadata


def completed(directory: Path) -> bool:
    try:
        return (read_json(directory / 'conditions.json').get('status') == 'completed'
                and read_json(directory / 'result.json').get('status') == 'completed'
                and (directory / 'bag/metadata.yaml').is_file()
                and any(p.is_file() for p in (directory / 'bag').glob('*.db3')))
    except ValueError:
        return False


def begin_trial(path: Path, metadata: dict, instruction: str, run: int):
    if (instruction, run) not in session_pairs(metadata):
        raise ValueError('予定されていない指示・試行番号です。')
    directory = path / instruction / str(run)
    if directory.is_symlink() or (path / instruction).is_symlink():
        raise ValueError('試行ディレクトリにシンボリックリンクは使えません。')
    if completed(directory):
        raise ValueError('完了済みの試行は再実行しません。')
    if directory.exists():
        condition = directory / 'conditions.json'
        pristine = (condition.is_file() and read_json(condition).get('status') == 'unattempted'
                    and all(p.name == 'conditions.json' for p in directory.iterdir()))
        if not pristine:
            history = path / '.history' / f'{instruction}-{run}'
            if history.is_symlink() or history.parent.is_symlink():
                raise ValueError('履歴ディレクトリにシンボリックリンクは使えません。')
            history.mkdir(parents=True, exist_ok=True)
            number = 1
            while True:
                archive = history / f'{number:04d}'
                try:
                    archive.mkdir()
                    break
                except FileExistsError:
                    number += 1
            # Replace only the empty directory atomically reserved above.
            try:
                directory.rename(archive)
            except OSError:
                archive.rmdir()
                raise
    directory.mkdir(parents=True, exist_ok=True)
    write(directory / 'conditions.json', trial_conditions(metadata, directory, instruction, run))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--ids')
    parser.add_argument('--trials', type=int)
    parser.add_argument('--controller', choices=tuple(CHECKPOINT_DIRS))
    parser.add_argument('--provider', choices=('openai', 'ollama'))
    parser.add_argument('--model')
    parser.add_argument('--base-url')
    parser.add_argument('--mcp-command')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--validate', action='store_true')
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--client-args', action='store_true')
    mode.add_argument('--controller-name', action='store_true')
    mode.add_argument('--begin', nargs=2, metavar=('ID', 'RUN'))
    args = parser.parse_args(argv)
    try:
        if not args.validate and not args.directory.is_dir():
            raise ValueError('初期化済みセッションが必要です。')
        config, metadata = validate_session(args.directory, resume=args.resume or not args.validate,
            ids=args.ids, trials=args.trials, controller=args.controller,
            provider=args.provider, model=args.model,
            base_url=args.base_url, mcp_command=args.mcp_command)
        if args.plan:
            for instruction, run in session_pairs(metadata):
                if not completed(args.directory / instruction / str(run)):
                    print(f'{instruction}\t{run}')
        elif args.client_args:
            for argument in ('--provider', config.provider, '--model', config.model, '--base-url', config.base_url):
                print(argument)
        elif args.controller_name:
            print(metadata.get('controller', 'ai6800'))
        elif args.begin:
            begin_trial(args.directory, metadata, args.begin[0], int(args.begin[1]))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(str(exc) if isinstance(exc, ValueError) else 'セッション操作に失敗しました。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
