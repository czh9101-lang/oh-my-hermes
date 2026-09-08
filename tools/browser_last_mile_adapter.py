"""Explicit local host adapter. No daemon, default invocation, or new dependency.

The caller supplies existing Playwright/Chromium paths and owns this context
manager. Only the Node host sees transient forms and request bytes.
"""
from collections import Counter
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
from tempfile import TemporaryDirectory
from time import time, perf_counter_ns


class LocalLastMileAdapter:
    adapter_id = 'local-last-mile-reference'
    adapter_version = '1'

    def __init__(self, playwright, chromium, operation):
        self.playwright, self.chromium, self.operation = playwright, chromium, operation
        self.process = None
        self.home = None
        self.deadline = 0
        self.calls = Counter()
        self.host_ns = Counter()
        self.log = []

    def __enter__(self):
        self.home = TemporaryDirectory(prefix='omh-effect-host-')
        try:
            self.process = subprocess.Popen(['node', str(Path(__file__).with_suffix('.mjs'))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True, env={**os.environ, 'OMH_EFFECT_PLAYWRIGHT':str(self.playwright),
                    'OMH_EFFECT_CHROMIUM':str(self.chromium), 'OMH_EFFECT_HOME':self.home.name,
                    'HOME':self.home.name, 'TMPDIR':self.home.name})
        except OSError:
            self.home.cleanup()
            raise
        return self

    def rpc(self, command, deadline=None):
        started = perf_counter_ns()
        assert self.process is not None and self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(json.dumps(command) + '\n')
        self.process.stdin.flush()
        with selectors.DefaultSelector() as ready:
            ready.register(self.process.stdout, selectors.EVENT_READ)
            if not ready.select(max(0, min(12, (deadline or self.deadline) - time()))):
                raise TimeoutError('host_callback_deadline')
        line = self.process.stdout.readline(65536)
        if not line:
            raise ConnectionError('host_disconnected')
        result = json.loads(line)
        self.calls[command['cmd']] += 1
        self.host_ns[command['cmd']] += perf_counter_ns() - started
        self.log.append({'callback':command['cmd'], 'status':'unknown' if 'error' in result else 'returned'})
        if 'error' in result:
            raise RuntimeError('host_action_unknown')
        return result['result']

    def capabilities(self):
        return {'schema_version':'browser_adapter_capabilities/v1', 'modes':['headless'],
                'channels':['semantic_state', 'screenshot', 'mutation_interception', 'upload'],
                'unsupported':['download'], 'mutation_interception':'last_mile',
                'limits':{'leases':1, 'tabs':1, 'actions':64, 'ttl_seconds':300,
                          'capability_seconds':60, 'elements':8, 'state_bytes':4096}}

    def start(self, lease_id, scope, deadline):
        self.deadline = min(deadline, time() + 60)
        return self.rpc({'cmd':'start', 'lease':lease_id, 'origin':scope['origins'][0],
                         'operation':self.operation, 'deadline':self.deadline}, self.deadline)

    def observe(self, lease_id, tab_id, deadline):
        return self.rpc({'cmd':'observe', 'lease':lease_id}, deadline)

    def act(self, lease_id, tab_id, revision, key, operation, deadline):
        if operation != 'read':
            raise ValueError('inert_read_only')
        observed = self.observe(lease_id, tab_id, deadline)
        return {'status':'observed' if observed['revision'] == revision else 'stale_state'}

    def preview(self, lease_id, handle, operation):
        return self.rpc({'cmd':'preview', 'lease':lease_id, 'handle':handle, 'operation':operation})

    def resume(self, lease_id, preview_ref, attempt_id):
        return self.rpc({'cmd':'resume', 'lease':lease_id, 'preview_ref':preview_ref, 'attempt_id':attempt_id})

    def abort(self, lease_id, preview_ref):
        return self.rpc({'cmd':'abort', 'lease':lease_id, 'preview_ref':preview_ref})

    def release(self, lease_id, deadline):
        return self.rpc({'cmd':'release', 'lease':lease_id}, deadline)

    def __exit__(self, *exc):
        assert self.process is not None
        before = process_snapshot()
        owned = {self.process.pid}
        while True:
            children = {pid for pid, (parent, _) in before.items() if parent in owned}
            if children <= owned:
                break
            owned.update(children)
        try:
            if self.process.stdin:
                self.process.stdin.close()  # EOF runs Node's browser-close finally.
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        finally:
            after = process_snapshot()
            remaining = {pid for pid in owned if pid in after and pid in before and before[pid][1] == after[pid][1]}
            for pid in list(remaining):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    remaining.discard(pid)
            self.cleanup = {'tracked_processes':len(owned), 'forced_kills':len(remaining),
                            'remaining_resources':[] if not remaining else ['forced_process_teardown']}
            for stream in (self.process.stdout, self.process.stderr):
                if stream:
                    stream.close()
            if self.home:
                self.home.cleanup()


def process_snapshot():
    """Explicit Darwin host process ownership evidence, never called from core."""
    output = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,command='], text=True, timeout=3)
    result = {}
    for line in output.splitlines():
        pid, parent, command = line.strip().split(None, 2)
        result[int(pid)] = (int(parent), command)
    return result
