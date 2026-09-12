"""Execute the shipped workflow block with a process-level gh fixture, never GitHub."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import shutil

ROOT = Path(__file__).resolve().parents[2]


def workflow_block() -> str:
    lines = (ROOT / '.github/workflows/release.yml').read_text().splitlines()
    start = lines.index('      - name: Create or verify immutable GitHub release asset')
    run = lines.index('        run: |', start) + 1
    end = next(i for i in range(run, len(lines)) if lines[i].startswith('      - name:'))
    return textwrap.dedent('\n'.join(lines[run:end]))


def exercise_workflow(root: Path, scenario: str) -> tuple[int, list[list[str]], str]:
    """Run only the publication block, with all external calls replaced at PATH."""
    root.mkdir(parents=True)
    binary = root / 'bin'
    binary.mkdir()
    stub = binary / 'gh'
    stub.write_bytes(b'#!/usr/bin/env bash\nexec "$QA_PYTHON" "$QA_GH_FIXTURE" "$@"\n')
    stub.chmod(0o755)
    notes = root / 'notes.md'
    notes.write_bytes(b'- fixture $HOME "quotes"\n\n```sh\n$(touch forbidden)\n```\n')
    if scenario != 'missing':
        (root / 'remote-body.md').write_bytes(notes.read_bytes() + (b'drift' if scenario == 'drift' else b''))
    (root / 'wheel.whl').write_bytes(b'fixture wheel')
    (root / 'evidence.json').write_text('{}')
    environment = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                       QA_ROOT=str(root), QA_SCENARIO=scenario,
                       QA_PYTHON=Path(sys.executable).as_posix(), QA_GH_FIXTURE=Path(__file__).resolve().as_posix(),
                       OMH_WHEEL=str(root / 'wheel.whl'), OMH_EVIDENCE_ASSET=str(root / 'evidence.json'),
                       OMH_NOTES_FILE=str(notes), RELEASE_TAG='v2.0.4', RELEASE_CHANNEL='stable',
                       GITHUB_REPOSITORY='fixture/repo', RUNNER_TEMP=str(root),
                       OMH_HOME=str(root / 'omh'), HERMES_HOME=str(root / 'hermes'))
    shell = shutil.which('bash')
    if shell is None:
        raise FileNotFoundError('workflow fixture requires bash')
    result = subprocess.run([shell, '-c', workflow_block()], cwd=ROOT, env=environment,
                            text=True, capture_output=True, timeout=60)
    calls = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()]
    return result.returncode, calls, result.stdout + result.stderr


def gh_fixture() -> int:
    root = Path(os.environ['QA_ROOT'])
    scenario = os.environ['QA_SCENARIO']
    args = sys.argv[1:]
    with (root / 'calls.jsonl').open('a') as handle:
        handle.write(json.dumps(args) + '\n')
    if args[:2] == ['release', 'view']:
        if scenario == 'auth':
            print('authentication failed', file=sys.stderr)
            return 1
        if not (root / 'remote-body.md').exists():
            print('release not found', file=sys.stderr)
            return 1
        if '--json' in args:
            field = args[args.index('--json') + 1]
            if field == 'body':
                print(json.dumps({'body': (root / 'remote-body.md').read_text()}))
            elif field == 'assets':
                print('0')
            else:
                return 2
        return 0
    if args[:2] == ['release', 'create']:
        if '--notes-file' not in args or '--generate-notes' in args:
            return 2
        if Path(args[args.index('--notes-file') + 1]).read_bytes() != (root / 'notes.md').read_bytes():
            return 2
        (root / 'remote-body.md').write_bytes(Path(args[args.index('--notes-file') + 1]).read_bytes())
        return 0
    if args[:2] == ['release', 'upload']:
        return 0
    if args[:1] == ['api']:
        if scenario == 'auth':
            return 1
        if '/releases/tags/' in args[1]:
            print(json.dumps({'message': 'Not Found', 'status': '404'}))
            return 1
        return 0
    return 2


if __name__ == '__main__':
    raise SystemExit(gh_fixture())
