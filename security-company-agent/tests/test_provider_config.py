"""Offline tests for provider-scoped configuration and fail-closed search."""
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brain_core.provider_config import (ConfigurationError, Provider, ProviderConfiguration,
                                        build_provider_configuration, safe_provider_status)
from brain_core import web_search


SYNTHETIC_SECRET = 'SYNTHETIC_SEARXNG_TOKEN_SHOULD_NEVER_LEAK_7F3A9C'
ENDPOINT = web_search.SEARXNG_APPROVED_ENDPOINT


class Response:
    def __init__(self, value):
        self.payload = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.payload[:size]


class ProviderConfigTests(unittest.TestCase):
    def good_values(self, *, approved='1'):
        return {'endpoint': ENDPOINT, 'api_token': SYNTHETIC_SECRET, 'approved': approved}

    def test_CFG01_missing_configuration_is_disabled(self):
        cfg = build_provider_configuration(Provider.SEARXNG, {}, environ={})
        self.assertFalse(cfg.configured)
        self.assertFalse(cfg.validated)
        self.assertFalse(cfg.availability_verified)
        self.assertFalse(cfg.capability_enabled)

    def test_CFG02_missing_endpoint_fails_closed(self):
        with self.assertRaisesRegex(ConfigurationError, 'endpoint is required'):
            build_provider_configuration(Provider.SEARXNG, {'api_token': SYNTHETIC_SECRET,
                                                             'approved': '1'})

    def test_CFG03_missing_token_fails_closed(self):
        with self.assertRaisesRegex(ConfigurationError, 'token is required'):
            build_provider_configuration(Provider.SEARXNG, {'endpoint': ENDPOINT,
                                                             'approved': '1'})

    def test_CFG04_invalid_schemes_hosts_paths_and_endpoints_fail_closed(self):
        for endpoint in ('http://sovereign-brain-search.onrender.com/search',
                         'https://attacker.example/search',
                         'https://sovereign-brain-search.onrender.com/other',
                         'https://user:pass@sovereign-brain-search.onrender.com/search',
                         'https://sovereign-brain-search.onrender.com/search?token=x'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ConfigurationError):
                build_provider_configuration(Provider.SEARXNG,
                    {'endpoint': endpoint, 'api_token': SYNTHETIC_SECRET, 'approved': '1'})

    def test_CFG05_valid_synthetic_gateway_config_has_separate_states(self):
        cfg = build_provider_configuration(Provider.SEARXNG, self.good_values())
        self.assertTrue(cfg.configured)
        self.assertTrue(cfg.validated)
        self.assertFalse(cfg.availability_verified)
        self.assertTrue(cfg.capability_enabled)
        self.assertEqual(cfg.provider, Provider.SEARXNG)

    def test_CFG06_unknown_settings_fail_closed(self):
        with self.assertRaisesRegex(ConfigurationError, 'Unsupported settings'):
            build_provider_configuration(Provider.SEARXNG,
                dict(self.good_values(), OPENAI_API_KEY='synthetic-other-provider-secret'))

    def test_CFG07_explicit_provider_values_override_only_that_provider_environment(self):
        env = {'SEARXNG_ENDPOINT': ENDPOINT, 'SEARXNG_API_TOKEN': 'environment-token',
               'SEARXNG_APPROVED': '0', 'OPENAI_API_KEY': 'synthetic-openai-secret'}
        cfg = build_provider_configuration(Provider.SEARXNG,
            {'endpoint': ENDPOINT, 'api_token': SYNTHETIC_SECRET, 'approved': '1'}, environ=env)
        self.assertEqual(cfg._adapter_secret('api_token'), SYNTHETIC_SECRET)
        self.assertTrue(cfg.capability_enabled)
        self.assertNotIn('OPENAI_API_KEY', cfg.as_dict())

    def test_CFG08_provider_configurations_are_independent(self):
        searx = build_provider_configuration(Provider.SEARXNG, self.good_values())
        openai = build_provider_configuration(Provider.OPENAI,
            {'api_key': 'synthetic-openai-secret', 'model': 'synthetic-model'})
        self.assertEqual(set(searx.as_dict()), {'endpoint', 'api_token', 'approved'})
        self.assertEqual(set(openai.as_dict()), {'api_key', 'model'})
        self.assertNotIn('api_key', searx.as_dict())
        self.assertNotIn('api_token', openai.as_dict())

    def test_CFG09_unrelated_provider_credentials_are_never_imported(self):
        env = {'SEARXNG_ENDPOINT': ENDPOINT, 'SEARXNG_API_TOKEN': SYNTHETIC_SECRET,
               'SEARXNG_APPROVED': '1', 'OPENAI_API_KEY': 'synthetic-openai-secret',
               'DASHSCOPE_API_KEY': 'synthetic-alibaba-secret'}
        cfg = build_provider_configuration(Provider.SEARXNG, environ=env)
        self.assertNotIn('OPENAI_API_KEY', repr(cfg))
        self.assertNotIn('DASHSCOPE_API_KEY', repr(cfg))
        self.assertNotIn('synthetic-openai-secret', repr(cfg))
        self.assertNotIn('synthetic-alibaba-secret', repr(cfg))

    def test_CFG10_invalid_approval_state_fails_closed(self):
        with self.assertRaisesRegex(ConfigurationError, 'approval flag must be 0 or 1'):
            build_provider_configuration(Provider.SEARXNG, self.good_values(approved='yes'))

    def test_AUTH01_research_disabled_by_default(self):
        cfg = build_provider_configuration(Provider.SEARXNG,
            {'endpoint': ENDPOINT, 'api_token': SYNTHETIC_SECRET, 'approved': '0'})
        self.assertTrue(cfg.configured)
        self.assertTrue(cfg.validated)
        self.assertFalse(cfg.capability_enabled)

    def test_AUTH02_valid_configuration_alone_does_not_execute(self):
        cfg = build_provider_configuration(Provider.SEARXNG, self.good_values(approved='0'))
        runner = lambda *_: self.fail('network must not execute')
        result = web_search.search('synthetic query', authorized=True, configuration=cfg, transport=runner)
        self.assertEqual(result['status'], 'not_approved')

    def test_AUTH03_provider_enabled_without_request_approval_is_denied(self):
        cfg = build_provider_configuration(Provider.SEARXNG, self.good_values())
        calls = []
        result = web_search.search('synthetic query', authorized=False, configuration=cfg,
                                   transport=lambda *args: calls.append(args))
        self.assertEqual(result['status'], 'unauthorized')
        self.assertEqual(calls, [])

    def test_AUTH04_rejected_request_generates_zero_network_calls(self):
        cfg = build_provider_configuration(Provider.SEARXNG, self.good_values())
        calls = []
        result = web_search.search('synthetic query', configuration=cfg,
                                   transport=lambda *args: calls.append(args))
        self.assertEqual(result['status'], 'unauthorized')
        self.assertFalse(calls)

    def test_AUTH05_authorized_synthetic_request_reaches_only_mocked_gateway(self):
        cfg = build_provider_configuration(Provider.SEARXNG, self.good_values())
        calls = []
        result = web_search.search('synthetic query', authorized=True, configuration=cfg,
            transport=lambda request, timeout: (calls.append(request), Response({'results': [
                {'title': 'Synthetic source', 'url': 'https://example.test/', 'content': 'Mock snippet'}]}))[1])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].full_url.split('?')[0], ENDPOINT)
        self.assertEqual(calls[0].get_header('Authorization'), f'Bearer {SYNTHETIC_SECRET}')

    def test_AUTH06_invalid_approval_state_prevents_execution(self):
        with self.assertRaises(ConfigurationError):
            build_provider_configuration(Provider.SEARXNG, self.good_values(approved='true'))

    def test_AUTH07_existing_one_time_approval_and_decline_tests_are_present(self):
        source = (Path(__file__).with_name('test_conversation_research_ui.py')).read_text(encoding='utf-8')
        self.assertIn('test_decline_button_never_calls_search', source)
        self.assertIn('test_approval_button_executes_exact_query_once_and_displays_citation', source)
        self.assertIn('test_production_runner_without_validated_configuration_is_blocked', source)

    def test_AUTH08_configuration_validation_never_calls_network(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('validation must not connect')):
            cfg = build_provider_configuration(Provider.SEARXNG, self.good_values())
        self.assertTrue(cfg.validated)
        self.assertFalse(cfg.availability_verified)

    def test_SEC01_synthetic_secret_never_appears_in_logs(self):
        cfg = build_provider_configuration(Provider.SEARXNG, self.good_values())
        with self.assertLogs('provider-config-test', level='INFO') as captured:
            logging.getLogger('provider-config-test').info('configuration=%r status=%r', cfg,
                safe_provider_status(Provider.SEARXNG, cfg))
        self.assertNotIn(SYNTHETIC_SECRET, '\n'.join(captured.output))

    def test_SEC02_synthetic_secret_never_appears_in_validation_errors(self):
        try:
            build_provider_configuration(Provider.SEARXNG,
                {'endpoint': 'https://bad.example/search', 'api_token': SYNTHETIC_SECRET,
                 'approved': '1'})
        except ConfigurationError as error:
            self.assertNotIn(SYNTHETIC_SECRET, str(error))
        else:
            self.fail('invalid configuration should fail')

    def test_SEC03_secret_is_excluded_from_object_representations(self):
        cfg = build_provider_configuration(Provider.SEARXNG, self.good_values())
        self.assertNotIn(SYNTHETIC_SECRET, repr(cfg))
        self.assertNotIn(SYNTHETIC_SECRET, str(cfg))
        self.assertNotIn(SYNTHETIC_SECRET, repr(web_search.web_search_configuration(
            configuration=cfg, environ={})))

    def test_SEC04_secret_is_excluded_from_diagnostics(self):
        cfg = build_provider_configuration(Provider.SEARXNG, self.good_values())
        diagnostic = safe_provider_status(Provider.SEARXNG, cfg)
        self.assertNotIn(SYNTHETIC_SECRET, repr(diagnostic))
        self.assertNotIn(SYNTHETIC_SECRET, json.dumps(diagnostic))

    def test_SEC05_secret_is_not_persisted_in_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = build_provider_configuration(Provider.SEARXNG, self.good_values())
            Path(directory, 'diagnostic.json').write_text(json.dumps(cfg.as_dict()), encoding='utf-8')
            self.assertNotIn(SYNTHETIC_SECRET, Path(directory, 'diagnostic.json').read_text(encoding='utf-8'))

    def test_SEC06_configuration_does_not_mutate_parent_environment(self):
        before = dict(os.environ)
        build_provider_configuration(Provider.SEARXNG, self.good_values(), environ={})
        self.assertEqual(dict(os.environ), before)

    def test_SEC07_searxng_configuration_does_not_leak_to_other_adapters(self):
        cfg = build_provider_configuration(Provider.SEARXNG, self.good_values())
        openai = build_provider_configuration(Provider.OPENAI,
            {'api_key': 'synthetic-openai-secret', 'model': 'synthetic-model'})
        self.assertNotIn(SYNTHETIC_SECRET, repr(openai))
        self.assertNotIn(SYNTHETIC_SECRET, json.dumps(openai.as_dict()))
        self.assertNotIn('api_token', openai.as_dict())
        self.assertIs(cfg.provider, Provider.SEARXNG)

    def test_SEC08_search_does_not_fallback_to_brave(self):
        cfg = build_provider_configuration(Provider.SEARXNG,
            {'endpoint': ENDPOINT, 'api_token': SYNTHETIC_SECRET, 'approved': '0'})
        with patch.dict(os.environ, {'BRAVE_API_KEY': 'synthetic-brave-secret'}, clear=False):
            calls = []
            result = web_search.search('query', authorized=True, configuration=cfg,
                                       transport=lambda *args: calls.append(args))
        self.assertEqual(result['status'], 'not_approved')
        self.assertEqual(calls, [])

    def test_SEC09_synthetic_secret_not_in_source_or_test_artifact(self):
        root = Path(__file__).resolve().parents[2]
        for path in root.rglob('*'):
            if not path.is_file() or '.git' in path.parts or '__pycache__' in path.parts:
                continue
            if path == Path(__file__):
                continue
            try:
                content = path.read_bytes()
            except OSError:
                continue
            self.assertNotIn(SYNTHETIC_SECRET.encode(), content, f'synthetic test token leaked into {path}')

    def test_SEC10_unauthorized_calls_and_config_validation_are_network_silent(self):
        cfg = build_provider_configuration(Provider.SEARXNG, self.good_values())
        with patch('urllib.request.urlopen', side_effect=AssertionError('external network forbidden')) as opener:
            web_search.search('query', authorized=False, configuration=cfg)
            build_provider_configuration(Provider.SEARXNG, self.good_values())
        opener.assert_not_called()

    def test_provider_status_distinguishes_configuration_validation_availability_and_enablement(self):
        cfg = build_provider_configuration(Provider.SEARXNG, self.good_values())
        state = safe_provider_status(Provider.SEARXNG, cfg)
        self.assertEqual(set(state), {'provider','configured','validated','availability_verified','enabled','status'})
        self.assertTrue(state['configured'] and state['validated'] and state['enabled'])
        self.assertFalse(state['availability_verified'])

    def test_non_searxng_config_cannot_be_used_as_gateway(self):
        cfg = build_provider_configuration(Provider.OPENAI,
            {'api_key': 'synthetic-openai-secret', 'model': 'synthetic-model'})
        result = web_search.search('query', authorized=True, configuration=cfg,
                                   transport=lambda *_: self.fail('must not call transport'))
        self.assertEqual(result['status'], 'invalid_provider_configuration')


if __name__ == '__main__':
    unittest.main()
