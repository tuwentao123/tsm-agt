from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.evaluate_verification_routing import (
    CASES, FIXTURES, FrozenSkillCatalog, fixture_file_state, history_for, independent_check, input_for, is_environment_probe, is_fixture_check,
    load_fixture, materialize, permitted, run_case, score_trace,
)
from tests.test_conversation_loop import ScriptedModel, text_response
from tsm_agt.adapters.local_skills import UserSkillsStore
from tsm_agt.adapters.skill_catalog import SkillCatalog


def event(sequence, kind, **payload):
    return SimpleNamespace(sequence=sequence, event_type=kind, payload=payload)


def call(identity, tool_name, **arguments):
    return event(0, 'tool.requested', call={'call_id': identity, 'name': tool_name, 'arguments': arguments})


def result(sequence, identity, ok=True, **data):
    return event(sequence, 'tool.completed', result={'call_id': identity, 'ok': ok, 'data': data})


class VerificationRoutingEvaluationTest(unittest.TestCase):
    def test_each_run_freezes_catalog_and_resources_without_rewriting_user_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UserSkillsStore(Path(directory) / 'skills')
            catalog = FrozenSkillCatalog(store)
            skills = catalog.discover()
            revision = catalog.model_data(skills)['revision']
            with patch.object(SkillCatalog, 'discover', side_effect=AssertionError('live discovery')), \
                    patch.object(SkillCatalog, 'read', side_effect=AssertionError('live resource')):
                self.assertEqual(catalog.discover(), skills)
                self.assertEqual(catalog.model_data(skills)['revision'], revision)
                self.assertIn('terminal', catalog.read(skills[0], 'references/workflow.md')['content'])
            self.assertFalse(store.directory.exists())

    def test_implicit_inputs_have_no_test_reminder_and_explicit_controls_are_separate(self):
        for case in CASES:
            fixture = load_fixture(FIXTURES / f'verification-{case}.json')
            text = input_for(fixture, 'implicit')
            self.assertNotIn('verify-change', text)
            self.assertNotIn('unittest', text)
            if fixture['behavior_change']:
                self.assertIn('verify-change', input_for(fixture, 'explicit'))
                self.assertNotEqual(input_for(fixture, 'explicit'), text)
            else:
                self.assertEqual(input_for(fixture, 'explicit'), text)
        short = load_fixture(FIXTURES / 'verification-ui-short.json')
        long = load_fixture(FIXTURES / 'verification-ui-long.json')
        self.assertEqual(short['files'], long['files'])
        self.assertGreater(len(history_for(long)), len(history_for(short)))
        self.assertTrue(all('.agent/projectInstructions.md' not in f['files'] for f in [short, long]))

    def test_grader_requires_relevant_terminal_result_after_last_edit_and_model_observation(self):
        events = [call('edit', 'core.apply_patch', path='page.py'), result(1, 'edit'),
                  call('check', 'core.run_command', cwd='.', argv=[sys.executable, '-m', 'unittest', 'test_flow']),
                  result(2, 'check', succeeded=True, exit_code=0)]
        self.assertFalse(score_trace(events, set())['model_verified_after_last_edit'])
        self.assertTrue(score_trace(events, {'check'})['model_verified_after_last_edit'])
        events += [call('edit-again', 'core.apply_patch', path='page.py'), result(3, 'edit-again')]
        self.assertFalse(score_trace(events, {'check'})['model_verified_after_last_edit'])
        unrelated = [call('edit', 'core.apply_patch'), result(1, 'edit'),
                     call('irrelevant', 'core.run_command', cwd='.', argv=[sys.executable, '-c', 'pass']),
                     result(2, 'irrelevant', succeeded=True, exit_code=0)]
        self.assertFalse(score_trace(unrelated, {'irrelevant'})['model_verified_after_last_edit'])

    def test_loading_a_workflow_is_not_verification(self):
        events = [call('skill', 'skill.load', name='verify-change'), result(1, 'skill'),
                  call('edit', 'core.apply_patch'), result(2, 'edit')]
        facts = score_trace(events, {'skill', 'edit'})
        self.assertTrue(facts['verification_skill_loaded'])
        self.assertFalse(facts['model_verified_after_last_edit'])

    def test_approval_preserves_tests_and_excludes_unrelated_commands(self):
        fixture = load_fixture(FIXTURES / 'verification-ui-short.json')
        self.assertTrue(permitted(SimpleNamespace(name='core.apply_patch', arguments={'path':'page.py'}), fixture))
        for name in ['test_flow.py', '../page.py', '/tmp/page.py']:
            self.assertFalse(permitted(SimpleNamespace(name='core.apply_patch', arguments={'path':name}), fixture))
        self.assertFalse(is_fixture_check({'cwd':'.','argv':[sys.executable,'-m','unittest','--help']}))
        self.assertFalse(is_fixture_check({'cwd':'.','argv':[sys.executable,'-m','unittest'],'mode':'background'}))
        self.assertTrue(is_fixture_check({'cwd':'.','argv':[sys.executable,'-m','pytest','test_flow.py','-q']}))
        self.assertFalse(is_fixture_check({'cwd':'.','argv':[sys.executable,'-m','pytest','--collect-only']}))
        with patch('scripts.evaluate_verification_routing.shutil.which', return_value='/observed/pytest'):
            self.assertTrue(is_fixture_check({'cwd':'.','argv':['/observed/pytest','test_flow.py','-q']}))
        self.assertFalse(is_fixture_check({'cwd':'.','argv':['/unobserved/pytest','test_flow.py']}))

    def test_runner_cache_does_not_hide_changes_to_protected_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.pytest_cache').mkdir()
            (root / '.pytest_cache' / 'README.md').write_text('generated cache')
            (root / 'test_flow.py').write_text('protected source')
            self.assertEqual(set(fixture_file_state(root)), {'test_flow.py'})

    def test_environment_discovery_is_allowed_but_is_not_verification(self):
        arguments = {'cwd': '.', 'argv': [sys.executable, '-c', 'import sys; print(sys.executable)']}
        self.assertTrue(is_environment_probe(arguments))
        self.assertFalse(is_fixture_check(arguments))
        self.assertFalse(is_environment_probe({'cwd': '.', 'argv': [sys.executable, '-c', 'import os; os.remove("README.md")']}))
        self.assertFalse(is_environment_probe({'cwd': '.', 'argv': [sys.executable, '-c', '__import__("os").system("anything")']}))

    def test_markdown_prompt_fixture_has_an_executable_contract(self):
        fixture = load_fixture(FIXTURES / 'verification-prompt-followup.json')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            materialize(fixture, root)
            self.assertFalse(independent_check(root)['passed'])
            path = root / 'planning.md'
            path.write_text(path.read_text().replace('Use core.todo_list to coordinate work.',
                'core.todo_list is a basic tool, not a Skill.'))
            self.assertTrue(independent_check(root)['passed'])

    @unittest.skipUnless(shutil.which('node'), 'Node.js unavailable')
    def test_javascript_fixture_tests_interleaving_instead_of_python_syntax(self):
        fixture = load_fixture(FIXTURES / 'verification-ui-short.json')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            materialize(fixture, root)
            self.assertFalse(independent_check(root)['passed'])
            (root / 'page.py').write_text('PAGE_SCRIPT = "function buildTimeline(events) { return [...events]; }"\n')
            self.assertTrue(independent_check(root)['passed'])


class VerificationRoutingRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_autonomous_routing_is_measured_without_forcing_continuation(self):
        model = ScriptedModel([text_response('unverified-final', '改完了。')])
        cfg = SimpleNamespace(base_url='http://unused.invalid', model='fixture', api_key='fixture',
            protocol='open', anthropic_version='', timeout_seconds=10, effective_output_token_parameter='max_tokens',
            strict_tool_schema=False, streaming=False, open_api_mode='chat', reasoning_effort=None, capability_overrides={})
        args = SimpleNamespace(prompt_template=None, reasoning_effort=None, label='unit', mode='implicit', timeout=10, no_stream=True)
        with patch('scripts.evaluate_verification_routing._physical_model_provider', return_value=model):
            report = await run_case(args, cfg, 'prompt-followup', 1)
        self.assertTrue(report['valid_run'])
        self.assertTrue(report['completed'])
        self.assertFalse(report['case_passed'])
        self.assertFalse(report['model_verified_after_last_edit'])
        self.assertEqual(report['tool_names'], [])
        self.assertEqual(report['reviewer_calls'], 0)
        self.assertEqual(len(model.requests), 1)
        self.assertTrue(any(m.message_id == 'tool-catalog-context-skill.load' for m in model.requests[0].messages))
        self.assertIn('verify-change', str(model.requests[0].tools))


if __name__ == '__main__':
    unittest.main()
