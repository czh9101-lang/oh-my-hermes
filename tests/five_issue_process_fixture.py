#!/usr/bin/env python3
"""Local process fixture; never runs a vendor or asserts native admission.

CLI: --mode plain|stdout|stderr|codex|claude --stdout-file PATH
--stderr-file PATH --exit-code N --session-id ID. Inputs are transient bytes.
Optional paired --control FIFO --events FIFO use ready -> start -> started ->
emitted -> finish -> finished. Each receive has --timeout seconds (default 5).
The portable helper uses an authenticated loopback socket instead of FIFOs.
Protocol init is emitted at start; final text/error at finish. Plain streams
are emitted before the finish barrier. --json and --output-format stream-json
select Codex/Claude shapes, not native executables; --version says fixture.

PYTHONPATH=tests: import after _local_package.load_local_package() when a test
also imports OMH. process_fixture() owns a child, FIFOs and transient inputs;
its finally reaps the child and removes state even when an assertion fails.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Generator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import BinaryIO, TypedDict
import uuid


class Cleanup(TypedDict):
    owned_resources: list[str]
    terminated_processes: list[int]
    removed_paths: list[str]
    verified_absent: bool
    errors: list[str]


def _line(stream: BinaryIO | socket.SocketIO, timeout: float) -> str:
    """Await one bounded control/event frame; no polling or fixed delays."""
    deadline = time.monotonic() + timeout
    data = bytearray()
    while len(data) < 64:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise TimeoutError('fixture_event_deadline')
        byte = stream.read(1)
        if not byte:
            raise EOFError('fixture_control_closed')
        if byte == b'\n':
            return data.decode('ascii')
        data.extend(byte)
    raise ValueError('fixture_control_frame_too_long')


def _fifo(stack: ExitStack, path: Path) -> BinaryIO:
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    stream = stack.enter_context(os.fdopen(fd, 'r+b', buffering=0))
    if not stat.S_ISFIFO(os.fstat(fd).st_mode):
        raise ValueError('fixture_requires_named_pipe')
    return stream


@dataclass(frozen=True, slots=True)
class FixtureProcess:
    """Owned child handles; cleanup receipt is filled by the context's finally."""
    process: subprocess.Popen[bytes]
    command: list[str]
    control: BinaryIO | socket.SocketIO
    events: BinaryIO | socket.SocketIO
    stdout: BinaryIO
    stderr: BinaryIO
    cleanup: Cleanup

    def send(self, command: str) -> None:
        if command not in ('start', 'finish'):
            raise ValueError('fixture_control_command_invalid')
        _ = self.control.write((command + '\n').encode('ascii'))

    def event(self, expected: str, timeout: float = 5) -> None:
        observed = _line(self.events, timeout)
        if observed != expected:
            raise ValueError('fixture_event_order_mismatch')

    def finish(self) -> subprocess.CompletedProcess[bytes]:
        self.send('finish')
        self.event('finished')
        code = self.process.wait(timeout=5)
        _ = self.stdout.seek(0)
        _ = self.stderr.seek(0)
        return subprocess.CompletedProcess(self.command, code, self.stdout.read(), self.stderr.read())


@contextmanager
def process_fixture(arguments: Sequence[str] = (), *, stdout: bytes = b'',
                    stderr: bytes = b'') -> Generator[FixtureProcess, None, None]:
    """Start a portably gated CLI; consume ready before send(start), then emitted.

    Output files avoid deadlocking oversized-output tests before finish. They
    and input bytes live only in an invocation-owned TemporaryDirectory.
    The helper itself does not exercise OMH's process runner; use the CLI with
    caller-owned FIFO paths for runner integration.
    """
    receipt: Cleanup = {'owned_resources': [], 'terminated_processes': [],
                        'removed_paths': [], 'verified_absent': False, 'errors': []}
    with ExitStack() as ownership:
        holder = tempfile.TemporaryDirectory(prefix='five-issue-fixture-')
        directory = holder.name
        root = Path(directory)
        receipt['owned_resources'].append(directory)
        process: subprocess.Popen[bytes] | None = None
        listener: socket.socket | None = None
        connection: socket.socket | None = None

        def record_cleanup() -> None:
            receipt['removed_paths'].append(directory)
            receipt['verified_absent'] = (
                not root.exists() and (process is None or process.returncode is not None)
                and all(endpoint is None or endpoint.fileno() == -1 for endpoint in (listener, connection))
            )

        # LIFO: close pipes/files, reap child, remove directory, then attest cleanup.
        _ = ownership.callback(record_cleanup)
        _ = ownership.enter_context(holder)
        with ExitStack() as stack:
            listener = stack.enter_context(socket.socket())
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(5)
            address: Callable[[], tuple[str, int]] = listener.getsockname
            port = address()[1]
            receipt['owned_resources'].append(f'tcp:127.0.0.1:{port}')
            token = uuid.uuid4().hex
            _ = (root / 'stdout.input').write_bytes(stdout)
            _ = (root / 'stderr.input').write_bytes(stderr)
            out = stack.enter_context((root / 'stdout.output').open('w+b'))
            err = stack.enter_context((root / 'stderr.output').open('w+b'))
            command = [sys.executable, str(Path(__file__).resolve()), *arguments,
                       '--control-port', str(port),
                       '--stdout-file', str(root / 'stdout.input'),
                       '--stderr-file', str(root / 'stderr.input')]
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                env={**os.environ, 'FIVE_ISSUE_CONTROL_TOKEN': token},
            )
            try:
                accepted = listener.accept()[0]
                connection = stack.enter_context(accepted)
                connection.settimeout(5)
                control = stack.enter_context(connection.makefile('rwb', buffering=0))
                if _line(control, 5) != token:
                    raise ValueError('fixture_control_identity_mismatch')
                child = FixtureProcess(process, command, control, control, out, err, receipt)
                yield child
            finally:
                if process.poll() is None:
                    process.kill()
                    receipt['terminated_processes'].append(process.pid)
                _ = process.wait(timeout=5)


def _emit(payload: dict[str, str | bool | list[str] | dict[str, str]]) -> None:
    print(json.dumps(payload), flush=True)


def _copy(path: Path | None, destination: BinaryIO) -> None:
    if path is not None:
        with path.open('rb') as source:
            shutil.copyfileobj(source, destination, length=65536)
    destination.flush()


class Arguments(argparse.Namespace):
    mode: str = 'plain'
    stdout_file: Path | None = None
    stderr_file: Path | None = None
    result_file: Path | None = None
    result_path: Path | None = None
    fenced_result: bool = False
    exit_code: int = 0
    session_id: str = ''
    control: Path | None = None
    events: Path | None = None
    control_port: int | None = None
    timeout: float = 5
    json: bool = False
    output_format: str | None = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('--version', action='version', version='five-issue-process-fixture 1')
    _ = parser.add_argument('--mode', choices=('plain', 'stdout', 'stderr', 'codex', 'claude'), default='plain')
    _ = parser.add_argument('--stdout-file', type=Path)
    _ = parser.add_argument('--stderr-file', type=Path)
    _ = parser.add_argument('--result-file', type=Path)
    _ = parser.add_argument('--result-path', type=Path)
    _ = parser.add_argument('--fenced-result', action='store_true')
    _ = parser.add_argument('--exit-code', type=int, choices=range(256), default=0)
    _ = parser.add_argument('--session-id')
    _ = parser.add_argument('--control', type=Path)
    _ = parser.add_argument('--events', type=Path)
    _ = parser.add_argument('--control-port', type=int)
    _ = parser.add_argument('--timeout', type=float, default=5)
    protocol = parser.add_mutually_exclusive_group()
    _ = protocol.add_argument('--json', action='store_true')
    _ = protocol.add_argument('--output-format', choices=('stream-json',))
    _ = parser.add_argument('--verbose', action='store_true')
    args = Arguments()
    args.session_id = str(uuid.uuid4())
    _ = parser.parse_args(argv, namespace=args)
    if bool(args.control) != bool(args.events):
        parser.error('--control and --events must be paired')
    if args.control_port is not None and (
        args.control is not None or not 1 <= args.control_port <= 65535
    ):
        parser.error('--control-port must be valid and cannot be combined with FIFOs')
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 60:
        parser.error('--timeout must be finite and in (0, 60]')
    mode = 'codex' if args.json else 'claude' if args.output_format else args.mode
    try:
        with ExitStack() as stack:
            if args.control_port is not None:
                token = os.environ.get('FIVE_ISSUE_CONTROL_TOKEN', '')
                if len(token) != 32 or any(char not in '0123456789abcdef' for char in token):
                    raise ValueError('fixture_control_identity_missing')
                connection = stack.enter_context(socket.create_connection(('127.0.0.1', args.control_port), 5))
                control = stack.enter_context(connection.makefile('rwb', buffering=0))
                _ = control.write((token + '\n').encode('ascii'))
                events = control
            else:
                control = _fifo(stack, args.control) if args.control else None
                events = _fifo(stack, args.events) if args.events else None
            if control is not None and events is not None:
                _ = events.write(b'ready\n')
                if _line(control, args.timeout) != 'start':
                    raise ValueError('fixture_expected_start')
                _ = events.write(b'started\n')
            if mode == 'codex':
                _emit({'type': 'thread.started', 'thread_id': args.session_id})
            elif mode == 'claude':
                _emit({'type': 'system', 'subtype': 'init', 'session_id': args.session_id})
            elif mode != 'stderr':
                _copy(args.stdout_file, sys.stdout.buffer)
            if mode != 'stdout':
                _copy(args.stderr_file, sys.stderr.buffer)
            if control is not None and events is not None:
                _ = events.write(b'emitted\n')
                if _line(control, args.timeout) != 'finish':
                    raise ValueError('fixture_expected_finish')
            if mode in ('codex', 'claude'):
                text = args.stdout_file.read_text(encoding='utf-8') if args.stdout_file else ''
                if mode == 'codex':
                    if args.exit_code:
                        _emit({'type': 'turn.failed', 'error': {'message': text}})
                    else:
                        _emit({'type': 'item.completed', 'item': {
                            'id': 'fixture-message', 'type': 'agent_message', 'text': text}})
                else:
                    result: dict[str, str | bool | list[str] | dict[str, str]] = {
                        'type': 'result', 'session_id': args.session_id,
                        'subtype': 'error_during_execution' if args.exit_code else 'success',
                        'is_error': bool(args.exit_code)}
                    result['errors' if args.exit_code else 'result'] = [text] if args.exit_code else text
                    _emit(result)
            if args.result_file is not None:
                if args.result_path is not None:
                    _ = shutil.copyfile(args.result_file, args.result_path)
                if args.fenced_result:
                    print('```json', flush=True)
                    _copy(args.result_file, sys.stdout.buffer)
                    print('\n```', flush=True)
            if events is not None:
                _ = events.write(b'finished\n')
        return args.exit_code
    except TimeoutError:
        print('fixture_control_timeout', file=sys.stderr)
        return 124
    except (OSError, ValueError, EOFError) as error:
        print('fixture_error:' + type(error).__name__, file=sys.stderr)
        return 2


def executor_main(executor: str, argv: list[str]) -> int:
    """Native-shaped argv adapter used only by local public-dispatch scenarios."""
    if '--version' in argv:
        print('five-issue-process-fixture 1.0')
        return 0
    if '--help' in argv:
        if '--fixture-unsupported-help' in argv:
            print('exec')
            return 0
        print('exec --json resume' if executor == 'codex' else
              '--output-format stream-json --verbose --resume')
        return 0
    if 'resume' in argv or any(arg.startswith('--resume') for arg in argv):
        raise AssertionError('fixture_resume_must_never_launch')
    import re
    prompt = argv[-1] if executor == 'codex' else argv[argv.index('-p') + 1]
    match = re.search(r'JSON sidecar to exactly (.+)\.', prompt)
    if match is None:
        raise ValueError('fixture_return_contract_missing')
    # The temporary sidecar path carries no unit identity; the frozen prompt does.
    fanout, unit = os.environ['OMH_FANOUT_LINEAGE'].rsplit('/', 1)[-1].split(':')
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True,
                          text=True, check=True).stdout.strip()
    sid = str(uuid.uuid5(uuid.NAMESPACE_URL, match[1]))
    if 'fixture-duplicate' in prompt:
        sid = '12345678-1234-4234-8234-123456789abc'
    if 'Work unit: Malformed' in prompt:
        sid = '--latest'
    payload: dict[str, object] = {'schema_version': 'fanout_unit_result/v1', 'unit_id': unit,
               'run_id': fanout + '-' + unit, 'fanout_id': fanout,
               'base_sha': head, 'head_sha': head, 'process_status': 'process_succeeded',
               'changed_paths': [], 'checks': [], 'findings': []}
    text = 'fixture failed' if '--fixture-fail' in argv else '```json\n' + json.dumps(payload) + '\n```'
    structured = '--json' in argv or '--output-format' in argv
    if not structured:
        print(text)
        return 0
    if executor == 'codex':
        _emit({'type': 'thread.started', 'thread_id': sid})
        _emit({'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'REASONING_PRIVATE_SENTINEL'}})
        _emit({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}})
        print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 12, 'output_tokens': 7}}))
    else:
        _emit({'type': 'system', 'subtype': 'init', 'session_id': sid, 'body': 'EVENT_PRIVATE_SENTINEL'})
        _emit({'type': 'result', 'session_id': sid, 'result': text,
               'usage': {'input_tokens': 'ignored'}})
        print(json.dumps({'type': 'result', 'session_id': sid, 'result': text,
                          'usage': {'input_tokens': 12, 'output_tokens': 7}, 'total_cost_usd': 0.02}))
    print(json.dumps({'type': 'thread.started', 'thread_id': 'ffffffff-ffff-4fff-8fff-ffffffffffff',
                      'body': 'PROMPT_PRIVATE_SENTINEL'}), file=sys.stderr)
    print('STDERR_PRIVATE_SENTINEL', file=sys.stderr)
    return 3 if '--fixture-fail' in argv else 0


if __name__ == '__main__':
    raise SystemExit(main())
