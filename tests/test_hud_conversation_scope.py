"""Session-local HUD rows must never borrow another conversation's work."""
from contextlib import closing
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from omh.plugin_bundle.omh.runtime_reader import read_omh_hud
from omh.tui_widget_pack import widget_payload
from test_plugin_hermes_delegation import NOW, PARENT_ID, _build_state_db, _write_manifest


class HudConversationScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hermes = self.root / '.hermes'
        self.hermes.mkdir()
        self.omh = self.root / '.omh'
        self.env = mock.patch.dict(os.environ, {'HOME': str(self.root), 'OMH_HOME': str(self.omh), 'HERMES_HOME': str(self.hermes)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = mock.patch('omh.plugin_bundle.omh.hermes_delegation.time.time', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def build(self, other_count=1):
        children = [{'id': 'child_own', 'model': 'gpt-5.6-sol', 'started_at': NOW - 30,
                     'usage': {'input_tokens': 100, 'output_tokens': 20, 'actual_cost_usd': 0.25, 'last_seen': NOW - 1}}]
        children += [{'id': f'child_other{i}', 'model': 'other-model', 'started_at': NOW - 10,
                      'usage': {'input_tokens': 9000, 'actual_cost_usd': 9, 'last_seen': NOW - 1}} for i in range(other_count)]
        _build_state_db(self.hermes, children)
        with closing(sqlite3.connect(self.hermes / 'state.db')) as db, db:
            db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?)', ('other-owner', 'other-model', '{}', NOW - 50))
            for i in range(other_count):
                db.execute('UPDATE sessions SET model_config=? WHERE id=?', (json.dumps({'_delegate_from': 'other-owner'}), f'child_other{i}'))

    def hud(self, **kwargs):
        return read_omh_hud(self.omh, self.hermes, **kwargs)

    def test_rows_and_derived_totals_are_owned_by_the_reading_conversation(self):
        self.build()
        own = self.hud(session_ref=PARENT_ID)['subagents']
        self.assertEqual([row['task_id'] for row in own['rows']], ['own'])
        self.assertEqual((own['active'], own['running'], own['blocked'], own['completed']), (1, 1, 0, 0))
        self.assertEqual(sum(row['tokens'] for row in own['rows']), 120)
        self.assertEqual(sum(row['cost_usd'] for row in own['rows']), 0.25)
        self.assertEqual([row['task_id'] for row in self.hud(session_ref='other-owner')['subagents']['rows']], ['other0'])
        self.assertEqual(len(self.hud()['subagents']['rows']), 2)

    def test_ownership_is_applied_before_the_native_row_cap(self):
        self.build(other_count=40)
        result = self.hud(session_ref=PARENT_ID)['subagents']
        self.assertEqual([row['task_id'] for row in result['rows']], ['own'])
        self.assertEqual(result['hidden_rows'], 0)
        self.assertEqual(result['active'], 1)

    def test_unknown_or_malformed_identity_does_not_select_an_owner(self):
        self.build()
        for reference in ('not-a-session', PARENT_ID + '\n', ' ' + PARENT_ID, PARENT_ID + '/' , 'x' * 161):
            for argument in ('session_ref', 'tui_session_ref'):
                with self.subTest(argument=argument, reference=reference):
                    result = self.hud(**{argument: reference})
                    self.assertEqual(result['subagents']['rows'], [])
                    self.assertEqual(result['subagents']['active'], 0)

    def test_compression_edges_keep_own_history_but_not_delegates_or_branches(self):
        self.build()
        with closing(sqlite3.connect(self.hermes / 'state.db')) as db, db:
            for column in ('parent_session_id TEXT', 'end_reason TEXT', 'source TEXT'):
                db.execute('ALTER TABLE sessions ADD COLUMN ' + column)
            db.execute("UPDATE sessions SET end_reason='compression', source='tui' WHERE id=?", (PARENT_ID,))
            for sid, parent, config, source in (
                ('continued', PARENT_ID, {}, 'tui'),
                ('branch', PARENT_ID, {'_branched_from': PARENT_ID}, 'tui'),
                ('tool-child', PARENT_ID, {}, 'tool'),
                ('real-child', PARENT_ID, {'_delegate_from': PARENT_ID}, 'tool'),
            ):
                db.execute('INSERT INTO sessions (id, model, model_config, started_at, parent_session_id, source) VALUES (?, ?, ?, ?, ?, ?)',
                           (sid, 'gpt-5.6-sol', json.dumps(config), NOW - 400, parent, source))
            for suffix, parent in (('continued', 'continued'), ('branch', 'branch'), ('nested', 'real-child'), ('tool', 'tool-child')):
                db.execute('INSERT INTO sessions (id, model, model_config, started_at) VALUES (?, ?, ?, ?)',
                           ('worker_' + suffix, 'gpt-5.6-sol', json.dumps({'_delegate_from': parent}), NOW - 3))
        for reference in (PARENT_ID, 'continued'):
            result = self.hud(session_ref=reference)
            self.assertEqual({row['task_id'] for row in result['subagents']['rows']}, {'own', 'continue', 'real-chi'})
        self.assertEqual({row['task_id'] for row in self.hud(session_ref='real-child')['subagents']['rows']}, {'nested'})

    def test_unowned_manifests_cannot_supply_labels_or_liveness(self):
        self.build()
        _write_manifest(self.hermes, 'unrelated-dispatch', ['Unrelated work'], started=NOW - 32, log_mtime=NOW + 50)
        result = next(row for row in self.hud(session_ref=PARENT_ID)['subagents']['rows'] if row['task_id'] == 'own')
        self.assertEqual(result['action'], '')
        self.assertEqual(result['delegation_id'], '')
        self.assertLessEqual(result['elapsed_seconds'], 30)

    def test_unowned_omh_and_maestro_rows_do_not_reenter_scoped_hud(self):
        self.build()
        status = {'active_executors': [
            {'target_id': 'foreign-run', 'executor_profile': profile, 'tokens_total': 9999, 'cost_usd': 55}
            for profile in ('hermes_local', 'maestro')],
            'latest_progress_events': [{'event_type': 'executor_completed'}], 'runs': []}
        result = self.hud(session_ref=PARENT_ID, status=status)
        self.assertEqual(result['maestro']['rows'], [])
        self.assertEqual([row['task_id'] for row in result['subagents']['rows']], ['own'])
        self.assertEqual(result['subagents']['active'], 1)
        self.assertEqual(result['subagents']['completed'], 0)
        self.assertEqual(result['graph'].get('nodes', []), [])

    def test_widget_unknown_identity_does_not_borrow_the_latest_todo(self):
        self.build()
        from omh.plugin_bundle.omh.todo_store import TODO_SCHEMA_VERSION, todo_path
        record = {'schema_version': TODO_SCHEMA_VERSION, 'session_ref': PARENT_ID,
                  'updated_at': '2027-01-15T08:00:00Z', 'title': 'Private plan',
                  'items': [{'text': 'Own task', 'state': 'active'}]}
        path = todo_path(self.omh, PARENT_ID)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record))
        with mock.patch('omh.plugin_bundle.omh.runtime_reader.live_tui_session_rows', return_value=[
            {'id': PARENT_ID, 'activity': NOW, 'started_at': NOW - 50}]), mock.patch(
                'omh.plugin_bundle.omh.runtime_reader._utc_epoch_now', return_value=NOW):
            result = self.hud(tui_session_ref='unmapped-transport')
        self.assertEqual(result['todo']['items'], [])

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the widget boundary')
    def test_widget_missing_malformed_and_unknown_ids_never_request_global_rows(self):
        # Run the shipped widget's actual Python reader, not a replica of its kwargs.
        self.build()
        with closing(sqlite3.connect(self.hermes / 'state.db')) as db, db:
            db.execute('UPDATE sessions SET started_at=? WHERE id LIKE ?', (time.time() - 2, 'child_%'))
        plugins = self.hermes / 'plugins'
        plugins.mkdir()
        source = Path(__file__).resolve().parents[1] / 'src/plugin_bundle/omh'
        shutil.copytree(source, plugins / 'omh')
        widget = self.root / 'widget.mjs'
        widget.write_bytes(widget_payload(Path(sys.executable)))
        script = """
import childProcess from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
childProcess.execFile = (exe, args, opts, cb) => {
  process.stdout.write(JSON.stringify({exe, args, env: opts.env})); cb(new Error('capture'));
};
syncBuiltinESMExports();
const {default: register} = await import(process.argv[1]);
register({Box: 'box', Text: 'text', h: () => null, defineWidgetApp: x => x,
          openWidget: () => null, updateWidget: () => null});
"""
        active = self.root / 'active.json'
        for reference in (None, 'bad/id', 'unmapped-transport', PARENT_ID, 'other-owner'):
            if reference is not None:
                active.write_text(json.dumps({'session_id': reference}))
            env = {**os.environ, 'HERMES_TUI_ACTIVE_SESSION_FILE': str(active)}
            capture = subprocess.run(['node', '--input-type=module', '-e', script, str(widget)], env=env, text=True, capture_output=True, check=True)
            invocation = json.loads(capture.stdout)
            read = subprocess.run([invocation['exe'], *invocation['args']], env=invocation['env'], text=True, capture_output=True, check=True)
            rows = json.loads(read.stdout)['subagents']['rows']
            expected = {PARENT_ID: ['own'], 'other-owner': ['other0']}.get(reference or '', [])
            self.assertEqual([row['task_id'] for row in rows], expected, reference)
