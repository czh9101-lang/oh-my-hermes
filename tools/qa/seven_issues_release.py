#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# How to run: install uv from https://docs.astral.sh/uv/ then, in this checkout:
# uv run python tools/qa/seven_issues_release.py --scenario local --output-dir /tmp/release-qa
"""Real local CLI and workflow-process scenarios; never publishes to GitHub."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Callable, TypedDict, assert_never
import runpy

ROOT = Path(__file__).resolve().parents[2]
exercise_workflow: Callable[[Path, str], tuple[int, list[list[str]], str]] = runpy.run_path(
    str(ROOT / 'tools/qa/_release_workflow_fixture.py')
)['exercise_workflow']


class CommandObservation(TypedDict):
    argv: list[str]
    exit: int
    stdout_sha256: str
    output: dict[str, object]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def invoke(arguments: list[str], environment: dict[str, str]) -> CommandObservation:
    command = [sys.executable, '-P', '-m', 'omh.cli', *arguments]
    result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120)
    payload = json.loads(result.stdout)
    return {'argv': command, 'exit': result.returncode,
            'stdout_sha256': hashlib.sha256(result.stdout.encode()).hexdigest(),
            'output': {key: payload[key] for key in ('status', 'verification', 'release_notes', 'publication_ready', 'blocking_failures', 'warnings', 'error') if key in payload}}


def local(output: Path, scratch: Path) -> dict[str, object]:
    environment = dict(os.environ, OMH_HOME=str(scratch / 'omh'), HERMES_HOME=str(scratch / 'hermes'))
    notes = output / 'notes.md'
    body = '- fixture $HOME "quotes"\n\n```md\n## Unreleased\n$(touch forbidden)\n```\n\n- caf\u00e9 \u2603  \n'
    history = '## 2.0.3 - 2026-09-12\r\n\r\n- historic\r\n'
    valid = output / 'valid'
    valid.mkdir()
    changelog = valid / 'CHANGELOG.md'
    changelog.write_bytes(('# Changelog\n\n## Unreleased\n\n' + body + '\n' + history).encode())
    subprocess.run(['git', 'init', '--quiet', str(valid)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(valid), 'add', 'CHANGELOG.md'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(valid), '-c', 'user.name=QA Fixture', '-c', 'user.email=qa@example.test',
                    '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'fixture'], check=True, capture_output=True,
                   env=dict(environment, GIT_AUTHOR_DATE='2026-09-13T00:00:00+0000', GIT_COMMITTER_DATE='2026-09-13T00:00:00+0000'))
    head = subprocess.check_output(['git', '-C', str(valid), 'rev-parse', 'HEAD'], text=True).strip()
    prepare = ['release', 'prepare', '--version', '2.0.4', '--repo-root', str(valid), '--notes-file', str(notes), '--json']
    before = digest(changelog)
    first = invoke(prepare, environment)
    assert first['exit'] == 0, first
    after = digest(changelog)
    assert before != after
    assert notes.read_bytes() == body.encode()
    assert changelog.read_bytes().endswith(history.encode())
    second = invoke(prepare, environment)
    assert second['exit'] == 0 and digest(changelog) == after, second
    # An interrupted stamp is represented by removing only the derived artifact.
    notes.unlink()
    resumed = invoke(prepare, environment)
    assert resumed['exit'] == 0 and digest(changelog) == after and notes.read_bytes() == body.encode(), resumed
    calls = [first, second, resumed]
    malformed = ('# Changelog\n', '## Unreleased\n\n', '## Unreleased\n- a\n## Unreleased\n- b\n',
                 '## Unreleased\n- new\n## 2.0.4 - 2026-09-13\n- old\n')
    invalid_outputs = []
    for index, source in enumerate(malformed):
        fixture = output / f'invalid-{index}'
        fixture.mkdir()
        path = fixture / 'CHANGELOG.md'
        path.write_bytes(source.encode())
        subprocess.run(['git', 'init', '--quiet', str(fixture)], check=True, capture_output=True)
        git_before = {entry.relative_to(fixture).as_posix(): digest(entry) for entry in (fixture / '.git').rglob('*') if entry.is_file()}
        current_notes = digest(notes)
        args = ['release', 'prepare', '--version', '2.0.4', '--repo-root', str(fixture), '--notes-file', str(notes), '--json']
        result = invoke(args, environment)
        assert result['exit'] == 2 and path.read_bytes() == source.encode() and digest(notes) == current_notes, result
        assert git_before == {entry.relative_to(fixture).as_posix(): digest(entry) for entry in (fixture / '.git').rglob('*') if entry.is_file()}
        invalid_outputs.append({'command': result, 'git_state_unchanged': True})
    for name, value, expected in (('matching-body', body, 0), ('drift-body', body + '\n', 1)):
        path = output / f'{name}.json'
        path.write_text(json.dumps({'body': value}), encoding='utf-8')
        result = invoke(['release', 'notes-verify', '--notes-file', str(notes), '--body-json', str(path), '--json'], environment)
        assert result['exit'] == expected, result
        calls.append(result)
    extracted = output / 'extracted.md'
    result = invoke(['release', 'notes', '--version', '2.0.4', '--repo-root', str(valid), '--notes-file', str(extracted), '--json'], environment)
    assert result['exit'] == 0 and extracted.read_bytes() == notes.read_bytes() and digest(changelog) == after, result
    calls.append(result)
    evidence = invoke(['release', 'evidence-bundle', '--version', '2.0.4', '--repo-root', str(valid), '--notes-file', str(notes), '--write', '--json'], environment)
    assert evidence['exit'] in (0, 1), evidence
    assert (evidence['exit'] == 0) == (evidence['output']['status'] == 'ready'), evidence
    metadata = evidence['output']['release_notes']
    assert isinstance(metadata, dict)
    assert metadata['sha256'] == 'sha256:' + digest(notes) and metadata['byte_length'] == notes.stat().st_size
    assert metadata['path'] == notes.name and metadata['version'] == '2.0.4'
    bundle = json.loads((scratch / 'omh/runtime/release-evidence/2.0.4.json').read_text())
    manifest = bundle['source_identity']['input_manifest']
    assert manifest['release_notes'] == metadata
    manifest_body = {key: value for key, value in manifest.items() if key != 'digest'}
    assert manifest['digest'] == 'sha256:' + hashlib.sha256(json.dumps(manifest_body, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    assert evidence['output']['publication_ready'] is False  # fixture is deliberately stamped but uncommitted
    calls.append(evidence)
    assert subprocess.check_output(['git', '-C', str(valid), 'rev-parse', 'HEAD'], text=True).strip() == head
    workflow = []
    for scenario in ('missing', 'matching', 'drift', 'auth'):
        code, argv, stdout = exercise_workflow(scratch / scenario, scenario)
        mutations = [args for args in argv if args[:2] in (['release', 'create'], ['release', 'edit'], ['release', 'upload'])]
        assert (code == 0) == (scenario in ('missing', 'matching')), (scenario, code, stdout)
        if scenario in ('drift', 'auth'):
            assert not mutations, argv
        if scenario == 'missing':
            assert '--notes-file' in mutations[0] and '--generate-notes' not in mutations[0], mutations
        if scenario == 'matching':
            assert argv[0][-2:] == ['--json', 'body'], argv
            assert any(args[:2] == ['release', 'upload'] for args in argv), argv
        workflow.append({'scenario': scenario, 'exit': code, 'argv': argv, 'output': stdout[-2500:]})
    return {'scenario': 'local', 'commands': calls, 'invalid_inputs': invalid_outputs, 'workflow': workflow,
            'changelog_sha256': {'before': before, 'stamped': after, 'rerun': digest(changelog)},
            'refs_unchanged': True, 'publication': 'not_run', 'provider_plugin': 'not_applicable'}


def live_body(output: Path, source: Path, scratch: Path) -> dict[str, object]:
    environment = dict(os.environ, OMH_HOME=str(scratch / 'omh'), HERMES_HOME=str(scratch / 'hermes'))
    payload = json.loads(source.read_text(encoding='utf-8'))
    assert isinstance(payload['body'], str)
    body = payload['body']
    calls = []
    for name, value, expected in (('exact', body, 0), ('changed', body + '\nsynthetic drift\n', 1)):
        notes = output / f'{name}.md'
        notes.write_bytes(value.encode())
        result = invoke(['release', 'notes-verify', '--notes-file', str(notes), '--body-json', str(source), '--json'], environment)
        assert result['exit'] == expected, result
        calls.append(result)
    authored = output / 'authored-2.0.3.md'
    result = invoke(['release', 'notes', '--version', '2.0.3', '--repo-root', str(ROOT), '--notes-file', str(authored), '--json'], environment)
    assert result['exit'] == 0, result
    calls.append(result)
    comparison = invoke(['release', 'notes-verify', '--notes-file', str(authored), '--body-json', str(source), '--json'], environment)
    assert comparison['exit'] in (0, 1), comparison
    calls.append(comparison)
    return {'scenario': 'live-body', 'commands': calls, 'authored_historical_body': comparison['output']['verification'],
            'publication': 'not_run', 'body_source': 'caller_supplied_gh_output'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=('local', 'live-body'), required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--body-json', type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    status = 0
    reaped = True
    with TemporaryDirectory(prefix='omh-release-qa-') as tmp:
        scratch = Path(tmp)
        try:
            match args.scenario:
                case 'local':
                    report = local(args.output_dir, scratch)
                case 'live-body':
                    if args.body_json is None:
                        parser.error('--body-json is required for live-body')
                    report = live_body(args.output_dir, args.body_json, scratch)
                case unreachable:
                    assert_never(unreachable)
        except (AssertionError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            status = 1
            reaped = not isinstance(exc, subprocess.TimeoutExpired)
            report = {'scenario': args.scenario, 'status': 'failed', 'error_category': type(exc).__name__, 'detail': str(exc)[:2048]}
    cleanup = {'verified_absent': not scratch.exists(), 'owned_processes_reaped': reaped}
    report['cleanup'] = cleanup
    if not cleanup['verified_absent']:
        status = 1
    encoded = json.dumps(report, sort_keys=True)
    (args.output_dir / 'result.json').write_text(encoded + '\n')
    print(encoded)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
