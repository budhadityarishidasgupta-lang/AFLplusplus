"""Offline tests for scoped child environments and launcher lifecycle."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brain_core.provider_config import ConfigurationError, Provider, build_provider_configuration
from brain_core import runtime_launcher as launcher
from brain_core.research_coordination import ResearchAuthorizationLedger, ResearchCoordinator
from brain_core.fallback_state import FallbackState


TOKEN = 'RUNTIME_SYNTHETIC_TOKEN_NEVER_PERSIST_91D67A'
ENDPOINT = 'https://sovereign-brain-search.onrender.com/search'


class RuntimeLauncherTests(unittest.TestCase):
    def configuration(self, *, approved='0'):
        return build_provider_configuration(Provider.SEARXNG, {
            'endpoint': ENDPOINT, 'api_token': TOKEN, 'approved': approved})

    def parent(self):
        return {
            'PATH': 'C:/Windows/System32;C:/Python', 'SYSTEMROOT': 'C:/Windows',
            'TEMP': 'C:/Temp', 'PYTHONPATH': 'safe-path',
            'OPENAI_API_KEY': 'synthetic-openai-not-for-child',
            'DASHSCOPE_API_KEY': 'synthetic-alibaba-not-for-child',
            'BRAVE_API_KEY': 'synthetic-brave-not-for-child',
            'SOVEREIGN_OPENAI_MODEL': 'synthetic-openai-model',
            'SOVEREIGN_ALIBABA_MODEL': 'synthetic-alibaba-model',
            'ALIBABA_QWEN_MODEL': 'synthetic-legacy-alibaba-model',
            'ALIBABA_BASE_URL': 'https://synthetic.invalid',
            'BRAIN_API_KEY': 'synthetic-brain-key-not-for-child',
            'OPENAI_BASE_URL': 'https://synthetic.invalid',
            'OTHER_SECRET': 'synthetic-other-not-for-child',
        }

    def test_RT01_missing_credentials_prevent_child_execution(self):
        cfg = build_provider_configuration(Provider.SEARXNG, {}, environ={})
        with self.assertRaises(ConfigurationError):
            launcher.build_child_environment(cfg, parent=self.parent())

    def test_RT02_invalid_provider_settings_prevent_child_execution(self):
        with self.assertRaises(ConfigurationError):
            build_provider_configuration(Provider.SEARXNG, {
                'endpoint': 'https://invalid.example/search', 'api_token': TOKEN, 'approved': '1'})

    def test_RT03_valid_synthetic_configuration_is_accepted(self):
        cfg = self.configuration()
        self.assertTrue(cfg.validated)
        child = launcher.build_child_environment(cfg, parent=self.parent())
        self.assertEqual(child['SEARXNG_APPROVED'], '0')
        with self.assertRaises(ConfigurationError):
            cfg.get('api_token')
        with self.assertRaises(ConfigurationError):
            launcher.build_child_environment(build_provider_configuration(
                Provider.OPENAI, {'api_key': 'synthetic', 'model': 'mock'}), parent=self.parent())

    def test_RT04_unknown_provider_fields_are_rejected(self):
        with self.assertRaises(ConfigurationError):
            build_provider_configuration(Provider.SEARXNG, {
                'endpoint': ENDPOINT, 'api_token': TOKEN, 'approved': '0',
                'OPENAI_API_KEY': 'synthetic'})

    def test_RT05_child_receives_only_approved_provider_settings(self):
        child = launcher.build_child_environment(self.configuration(), parent=self.parent())
        self.assertEqual(child['SEARXNG_ENDPOINT'], ENDPOINT)
        self.assertEqual(child['SEARXNG_API_TOKEN'], TOKEN)
        self.assertNotIn('OTHER_SECRET', child)

    def test_RT06_required_windows_runtime_variables_are_preserved(self):
        child = launcher.build_child_environment(self.configuration(), parent=self.parent())
        for key in ('SYSTEMROOT', 'TEMP', 'PATH'):
            self.assertIn(key, child)

    def test_RT07_RT08_RT09_unrelated_credentials_excluded(self):
        child = launcher.build_child_environment(self.configuration(), parent=self.parent())
        self.assertEqual(child['OPENAI_API_KEY'], '')
        self.assertNotIn('DASHSCOPE_API_KEY', child)
        self.assertEqual(child['BRAVE_API_KEY'], '')
        self.assertEqual(child['SOVEREIGN_OPENAI_MODEL'], '')
        self.assertEqual(child['SOVEREIGN_ALIBABA_MODEL'], '')
        for key in ('ALIBABA_QWEN_MODEL', 'ALIBABA_BASE_URL', 'OPENAI_BASE_URL'):
            self.assertEqual(child[key], '')

    def test_RT10_parent_environment_unchanged(self):
        parent = self.parent()
        before = dict(parent)
        launcher.build_child_environment(self.configuration(), parent=parent)
        self.assertEqual(parent, before)
        current = dict(os.environ)
        launcher.build_child_environment(self.configuration(), parent={})
        self.assertEqual(dict(os.environ), current)

    def test_SEC01_secret_is_not_in_command_arguments(self):
        popen = Mock(return_value=Mock())
        launcher.launch_studio(self.configuration(approved='1'), parent=self.parent(),
                               repository_root=Path('C:/isolated'), popen=popen)
        args, kwargs = popen.call_args
        self.assertNotIn(TOKEN, args[0])
        self.assertNotIn(TOKEN, repr({'cwd': kwargs['cwd'], 'shell': kwargs['shell']}))

    def test_SEC02_secret_is_not_printed_by_input_or_launcher(self):
        with patch('builtins.print') as output:
            cfg = launcher.read_searxng_configuration(input_fn=lambda _: TOKEN)
            child = launcher.build_child_environment(cfg, parent=self.parent())
        output.assert_not_called()
        self.assertNotIn(TOKEN, repr(cfg))
        # The child environment is deliberately secret-bearing, not a diagnostic.
        self.assertNotIn(TOKEN, repr(launcher._fingerprint(cfg)))
        del child

    def test_SEC03_SEC08_errors_do_not_reveal_secret(self):
        with self.assertRaises(ConfigurationError) as caught:
            launcher.read_searxng_configuration(
                input_fn=lambda _: (_ for _ in ()).throw(RuntimeError(TOKEN)))
        self.assertNotIn(TOKEN, str(caught.exception))
        with self.assertRaises(launcher.RuntimeLaunchError) as caught:
            launcher.launch_studio(self.configuration(approved='1'), parent=self.parent(),
                popen=Mock(side_effect=OSError(TOKEN)))
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_SEC04_diagnostic_representations_redact_secret(self):
        cfg = self.configuration()
        self.assertNotIn(TOKEN, repr(cfg))
        self.assertNotIn(TOKEN, str(cfg))
        self.assertNotIn(TOKEN, repr(launcher._fingerprint(cfg)))

    def test_SEC05_SEC06_no_configuration_file_or_repository_persistence(self):
        root = Path(__file__).resolve().parents[1]
        before = {p.relative_to(root) for p in root.rglob('*')
                  if p.is_file() and '.git' not in p.parts and '__pycache__' not in p.parts}
        launcher.build_child_environment(self.configuration(), parent=self.parent())
        after = {p.relative_to(root) for p in root.rglob('*')
                 if p.is_file() and '.git' not in p.parts and '__pycache__' not in p.parts}
        self.assertEqual(before, after)
        self.assertFalse((root / '.env').exists())

    def test_PROC01_harmless_real_child_receives_scoped_environment(self):
        child_code = ('import os,json; print(json.dumps({"token_present": bool(os.environ.get("SEARXNG_API_TOKEN")), '
                      '"openai": os.environ.get("OPENAI_API_KEY"), "local": os.environ.get("BRAIN_API_BASE")}))')
        cfg = self.configuration()
        parent = self.parent()
        # Real synthetic subprocess, Python stdlib only; no network or Studio.
        result = subprocess.run([sys.executable, '-c', child_code],
            env=launcher.build_child_environment(cfg, parent=parent),
            capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn('"token_present": true', result.stdout)
        self.assertIn('"openai": ""', result.stdout)
        self.assertIn('127.0.0.1:11434', result.stdout)

    def test_PROC02_shutdown_is_limited_to_owned_handle(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        process.returncode = 0
        result = launcher.shutdown_child(process)
        self.assertEqual(result, 0)
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once()
        process.kill.assert_not_called()

    def test_PROC03_failed_creation_is_sanitized(self):
        with self.assertRaises(launcher.RuntimeLaunchError) as caught:
            launcher.launch_studio(self.configuration(approved='1'), parent=self.parent(),
                popen=Mock(side_effect=OSError('creation failure')))
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_PROC04_timeout_is_sanitized_and_process_run_api_owns_wait(self):
        with self.assertRaises(launcher.RuntimeLaunchError):
            launcher.run_child(['python', '-c', 'pass'], self.configuration(),
                parent=self.parent(), run=Mock(side_effect=subprocess.TimeoutExpired('python', 1)))

    def test_PROC05_parent_environment_unchanged_after_child_helper(self):
        parent = self.parent()
        before = dict(parent)
        launcher.run_child(['python', '-c', 'pass'], self.configuration(),
                           parent=parent, run=Mock(return_value=Mock(returncode=0)))
        self.assertEqual(parent, before)

    def test_PROC06_never_targets_ollama_process_or_service(self):
        process_factory = Mock(return_value=Mock())
        launcher.launch_studio(self.configuration(approved='1'), parent=self.parent(),
                               popen=process_factory)
        command = process_factory.call_args.args[0]
        self.assertIn('streamlit', command)
        self.assertFalse(any('ollama' in str(part).casefold() for part in command))

    def test_PROC07_tests_never_launch_real_studio(self):
        process_factory = Mock(return_value=Mock())
        # Construction mocked; do not call Streamlit or run Studio.
        launcher.launch_studio(self.configuration(approved='1'), parent=self.parent(),
                               repository_root=Path('C:/synthetic'), popen=process_factory)
        self.assertIn('brain_studio.py', process_factory.call_args.args[0][4])
        self.assertIn('--server.port', process_factory.call_args.args[0])
        self.assertIn('0', process_factory.call_args.args[0])
        self.assertIn('--browser.gatherUsageStats', process_factory.call_args.args[0])

    def test_disabled_research_cannot_launch_studio(self):
        with self.assertRaises(ConfigurationError):
            launcher.launch_studio(self.configuration(approved='0'), parent=self.parent(), popen=Mock())

    def test_AUTH01_through_AUTH05_provider_configuration_does_not_replace_request_grant(self):
        cfg = self.configuration(approved='1')
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            ledger = ResearchAuthorizationLedger(Path(temp) / 'ledger.sqlite3')
            coordinator = ResearchCoordinator(reasoning_runner=lambda **_: FallbackState.FAILED,
                                              search_runner=lambda *a, **k: calls.append(a) or {
                                                  'status': 'ok', 'results': []},
                                              authorization_ledger=ledger)
            missing = coordinator.run(query='sensitive query', search_configuration=cfg,
                                      reasoning_authorized=False)
            self.assertEqual(missing.research_status, 'awaiting_authorization')
            coordinator.run(query='sensitive query', research_authorized=True,
                authorization_id='synthetic-grant', journey_id='synthetic-journey',
                reasoning_authorized=False, search_configuration=cfg)
            duplicate = coordinator.run(query='sensitive query', research_authorized=True,
                authorization_id='synthetic-grant', journey_id='synthetic-journey',
                reasoning_authorized=False, search_configuration=cfg)
        self.assertEqual(calls, [('sensitive query',)])
        self.assertEqual(duplicate.research_status, 'duplicate')

    def test_AUTH02_through_AUTH04_disabled_and_invalid_runtime_never_call_provider(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            ledger = ResearchAuthorizationLedger(Path(temp) / 'ledger.sqlite3')
            coordinator = ResearchCoordinator(reasoning_runner=lambda **_: FallbackState.FAILED,
                search_runner=lambda *a, **k: calls.append(a) or {'status': 'ok', 'results': []},
                authorization_ledger=ledger)
            disabled = self.configuration(approved='0')
            result = coordinator.run(query='query', research_authorized=True,
                authorization_id='grant', journey_id='journey', reasoning_authorized=False,
                search_configuration=disabled)
            self.assertEqual(result.research_status, 'not_approved')
        self.assertEqual(calls, [])

    def test_AUTH08_local_mode_child_disables_cloud_fallback(self):
        child = launcher.build_child_environment(self.configuration(), parent=self.parent())
        self.assertEqual(child['SOVEREIGN_LOCAL_MODE'], '1')
        self.assertEqual(child['SOVEREIGN_ALLOW_CLOUD_FALLBACK'], '0')
        self.assertEqual(child['OPENAI_API_KEY'], '')
        self.assertNotIn('DASHSCOPE_API_KEY', child)
        self.assertEqual(child['BRAVE_API_KEY'], '')
        self.assertEqual(child['ALIBABA_QWEN_MODEL'], '')
        self.assertEqual(child['ALIBABA_BASE_URL'], '')
        self.assertEqual(child['OPENAI_BASE_URL'], '')


if __name__ == '__main__':
    unittest.main()
