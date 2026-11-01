"""Write allowlisted evaluation conditions without exporting the host environment."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from llm.config import ClientConfig, ROOT
from llm.recording import load_instructions, planned_pairs


CHECKPOINT_DIRS = {'ai6800': 'go2_flat6800', 'ai15000': 'go2_flat15000'}


def checkpoint_path(controller='ai6800'):
    if controller not in CHECKPOINT_DIRS:
        raise ValueError('方策は ai6800 または ai15000 を指定してください。')
    return ROOT / 'ws/models' / CHECKPOINT_DIRS[controller] / 'checkpoint.pt'


def checkpoint_sha(controller='ai6800'):
    path = checkpoint_path(controller)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def dependencies():
    versions = {}
    for package in ('openai', 'openai-agents', 'rosbags'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def write(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def trial_conditions(shared, directory, instruction_id, run):
    allowed = {*ClientConfig('openai', '', '', '').public_conditions(), 'instruction_version',
               'controller', 'headless', 'world_init_x', 'checkpoint_sha256', 'dependencies', 'recorded_at'}
    return {**{key: value for key, value in shared.items() if key in allowed},
            'instruction_id': instruction_id, 'run': run, 'status': 'unattempted',
            'failure_reason': None, 'initial_pose': None, 'final_pose': None,
            'bag_path': str(directory / 'bag'), 'llm_response_s': None, 'motion_start_s': None,
            'total_wall_s': None, 'motion_sim_s': None}


def initialize_session(session: Path, config: ClientConfig, *, ids=None, trials=None,
                       controller='ai6800'):
    pairs = planned_pairs(ids=ids, trials=trials)
    sha = checkpoint_sha(controller)
    session.mkdir(parents=True, exist_ok=False)
    instructions = load_instructions()
    shared = {**config.public_conditions(), 'instruction_version': instructions['version'],
              'controller': controller, 'headless': True, 'world_init_x': -5.0,
              'checkpoint_sha256': sha, 'dependencies': dependencies(),
              'recorded_at': datetime.now(timezone.utc).isoformat()}
    selection = {
        'instruction_ids': list(dict.fromkeys(instruction for instruction, _ in pairs)),
        'trials_per_instruction': 3 if trials is None else trials}
    write(session / 'session.json', {**shared, **selection, 'planned_runs': len(pairs),
          'instruction_set_sha256': hashlib.sha256((ROOT / 'llm/instructions.json').read_bytes()).hexdigest(),
          'criteria': instructions['criteria']})
    for instruction, run in pairs:
        directory = session / instruction / str(run)
        directory.mkdir(parents=True)
        write(directory / 'conditions.json', trial_conditions(shared, directory, instruction, run))


def update_conditions(directory, status, *, initial=None, final=None, metadata=None, attempt=None,
                      preparation=None, reason=None):
    path = directory / 'conditions.json'
    data = json.loads(path.read_text())
    data = {**data, 'status': status}
    if status == 'preparing':
        cleared = ('failure_reason', 'initial_pose', 'final_pose', 'llm_response_s', 'motion_start_s',
                   'total_wall_s', 'motion_sim_s', 'input_wall_ns', 'evaluation_status',
                   'max_publish_gap_s', 'segment_boundaries_sim_s', 'preparation_diagnostics')
        data = {**data, **dict.fromkeys(cleared), 'recorder_started_monotonic': time.monotonic(),
                'recorder_started_at': datetime.now(timezone.utc).isoformat()}
    diagnostics = None
    if preparation and preparation.is_file():
        diagnostics = json.loads(preparation.read_text())
        if not isinstance(diagnostics, dict):
            raise ValueError('preparation diagnostics must be an object')
        data = {**data, 'preparation_diagnostics': diagnostics}
        if attempt is not None:
            previous = data.get('preparation_attempts', [])
            entries = [entry for entry in previous if entry.get('attempt') != attempt]
            data = {**data, 'preparation_attempts': sorted(
                [*entries, {**diagnostics, 'attempt': attempt}], key=lambda entry: entry['attempt'])}
    for key, source in (('initial_pose', initial), ('final_pose', final)):
        if source and source.is_file():
            data = {**data, key: json.loads(source.read_text()).get('pose')}
    if metadata and metadata.is_file():
        # This file is produced by the container metadata helper, never by the LLM.
        container = json.loads(metadata.read_text())
        data = {**data, 'base_commit': container.get('base_commit'),
                'rosbridge_version': container.get('rosbridge_version')}
    if attempt is not None:
        data = {**data, 'attempt': attempt}
    if status == 'client_result':
        result_path = directory / 'result.json'
        if result_path.is_file():
            result = json.loads(result_path.read_text())
            for key in ('status', 'failure_reason', 'llm_response_s', 'motion_start_s', 'total_wall_s', 'input_wall_ns'):
                data = {**data, key: result.get(key)}
        else:
            data = {**data, 'status': 'client_failed', 'failure_reason': 'result_missing'}
    elif status == 'prepared':
        data = {**data, 'failure_reason': None}
    elif status not in ('preparing', 'running', 'unattempted'):
        diagnostic_reason = (diagnostics or {}).get('failure_reason') or (diagnostics or {}).get('reason')
        data = {**data, 'failure_reason': reason or diagnostic_reason or status}
    if reason is not None:
        data = {**data, 'failure_reason': reason}
    if data.get('recorder_started_monotonic') is not None:
        data = {**data, 'recorder_wall_s': time.monotonic() - data['recorder_started_monotonic']}
    write(path, data)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    p.add_argument('--initialize', action='store_true')
    p.add_argument('--ids')
    p.add_argument('--trials', type=int)
    p.add_argument('--controller', choices=tuple(CHECKPOINT_DIRS), default='ai6800')
    p.add_argument('--provider', choices=('openai', 'ollama'))
    p.add_argument('--model')
    p.add_argument('--base-url')
    p.add_argument('--mcp-command')
    p.add_argument('--status', default='unattempted')
    p.add_argument('--initial', type=Path)
    p.add_argument('--final', type=Path)
    p.add_argument('--metadata', type=Path)
    p.add_argument('--attempt', type=int)
    p.add_argument('--preparation', type=Path)
    p.add_argument('--reason')
    args = p.parse_args(argv)
    if args.initialize:
        config = ClientConfig.from_env(provider=args.provider, model=args.model,
                                       base_url=args.base_url, mcp_command=args.mcp_command)
        initialize_session(args.directory, config, ids=args.ids,
                           trials=args.trials, controller=args.controller)
    else:
        update_conditions(args.directory, args.status, initial=args.initial, final=args.final,
                          metadata=args.metadata, attempt=args.attempt,
                          preparation=args.preparation, reason=args.reason)


if __name__ == '__main__':
    main()
