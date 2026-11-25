"""Mock-only tests for the permission-gated Brave and SearXNG adapters."""
from pathlib import Path
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brain_core import web_search


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return json.dumps(self.payload).encode()[:size]


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        self.gateway_env = {
            'SEARXNG_ENDPOINT': web_search.SEARXNG_APPROVED_ENDPOINT,
            'SEARXNG_API_TOKEN': 'synthetic-gateway-token',
            'SEARXNG_APPROVED': '1',
        }

    def test_configuration_requires_exact_approved_gateway_token_and_flag(self):
        for env in ({}, {'SEARXNG_ENDPOINT': web_search.SEARXNG_APPROVED_ENDPOINT,
                         'SEARXNG_API_TOKEN': 'synthetic-gateway-token', 'SEARXNG_APPROVED': '0'},
                    {'SEARXNG_ENDPOINT': 'https://attacker.example/search',
                     'SEARXNG_API_TOKEN': 'synthetic-gateway-token', 'SEARXNG_APPROVED': '1'},
                    {'SEARXNG_ENDPOINT': web_search.SEARXNG_APPROVED_ENDPOINT,
                     'SEARXNG_APPROVED': '1'}):
            config = web_search.web_search_configuration(environ=env)
            self.assertFalse(config.searxng_configured)
            self.assertFalse(config.configured)
            self.assertNotIn('synthetic-gateway-token', repr(config))
        approved = web_search.web_search_configuration(environ=self.gateway_env)
        self.assertTrue(approved.searxng_configured)
        self.assertFalse(approved.api_key_configured)
        self.assertEqual(approved.provider, 'searxng')
        self.assertNotIn('synthetic-gateway-token', repr(approved))

    def test_unapproved_gateway_does_not_search(self):
        config = web_search.web_search_configuration(environ={
            'SEARXNG_ENDPOINT': web_search.SEARXNG_APPROVED_ENDPOINT,
            'SEARXNG_API_TOKEN': 'synthetic-gateway-token', 'SEARXNG_APPROVED': '0'})
        transport = lambda *_: self.fail('transport must not run')
        result = web_search.search('synthetic query', authorized=True,
                                   configuration=config, transport=transport)
        self.assertEqual(result, {'status': 'not_approved', 'results': [], 'error': None})
        result = web_search.search('synthetic query', authorized=True, transport=transport,
                                   configuration=web_search.WebSearchConfiguration(
                                       api_key_configured=False, searxng_configured=True,
                                       searxng_endpoint='https://attacker.example/search',
                                       searxng_api_token='synthetic-gateway-token'))
        self.assertEqual(result['status'], 'invalid_endpoint')

    def test_unauthorized_gateway_request_never_calls_transport(self):
        config = web_search.web_search_configuration(environ=self.gateway_env)
        result = web_search.search('synthetic query', configuration=config,
                                   transport=lambda *_: self.fail('transport must not run'))
        self.assertEqual(result, {'status': 'unauthorized', 'results': [], 'error': None})

    def test_gateway_with_brave_credential_still_selects_only_gateway(self):
        env = dict(self.gateway_env, BRAVE_API_KEY='synthetic-brave-key')
        config = web_search.web_search_configuration(environ=env)
        self.assertEqual(config.provider, 'searxng')
        # Availability of a legacy credential does not cause provider fallback;
        # it is never copied into the scoped SearXNG configuration.
        scoped = web_search.searxng_configuration(environ=env)
        self.assertNotIn('api_key', scoped.as_dict())
        self.assertEqual(scoped.provider.value, 'searxng')

    def test_gateway_success_uses_fixed_host_bearer_token_and_result_limit(self):
        payload = {'results': [
            {'title': f'Result {index}', 'url': f'https://example{index}.test/page',
             'content': f'Synthetic unverified snippet {index}'} for index in range(8)]}
        calls = []

        def transport(request, timeout):
            calls.append((request, timeout))
            return FakeResponse(payload)

        config = web_search.web_search_configuration(environ=self.gateway_env)
        with patch.dict(os.environ, {'SEARXNG_API_TOKEN': 'environment-should-not-win'}, clear=False):
            result = web_search.search('synthetic query', authorized=True, transport=transport,
                                       max_results=99, timeout_seconds=300, configuration=config)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['results']), web_search.MAX_RESULTS)
        self.assertEqual(result['results'][0]['source']['provider'], 'searxng')
        self.assertEqual(calls[0][0].full_url.split('?')[0], web_search.SEARXNG_APPROVED_ENDPOINT)
        self.assertEqual(calls[0][0].get_header('Authorization'), 'Bearer synthetic-gateway-token')
        self.assertLessEqual(calls[0][1], web_search.DEFAULT_TIMEOUT_SECONDS)
        self.assertNotIn('synthetic-gateway-token', repr(result))
        self.assertNotIn('synthetic-gateway-token', calls[0][0].full_url)

    def test_empty_results_are_empty_and_malformed_records_are_rejected(self):
        config = web_search.web_search_configuration(environ=self.gateway_env)
        empty = web_search.search('query', authorized=True, configuration=config,
                                  transport=lambda *_: FakeResponse({'results': []}))
        malformed = web_search.search('query', authorized=True, configuration=config,
                                      transport=lambda *_: FakeResponse({'results': [
                                          {'title': 'bad', 'url': 'javascript:alert(1)', 'content': 'x'},
                                          {'title': 'credentials', 'url': 'https://user:pass@example.test/', 'content': 'x'}]}))
        self.assertEqual(empty['status'], 'empty')
        self.assertEqual(malformed['status'], 'empty')
        self.assertEqual(malformed['results'], [])

    def test_http_auth_rate_limit_and_upstream_errors_are_sanitized(self):
        config = web_search.web_search_configuration(environ=self.gateway_env)

        def failure(status):
            def raise_error(request, timeout):
                raise urllib.error.HTTPError(request.full_url, status, 'private synthetic detail',
                                             {'Authorization': 'Bearer synthetic-gateway-token'}, io.BytesIO())
            return raise_error

        for status in (401, 429, 502):
            with self.subTest(status=status):
                result = web_search.search('synthetic query', authorized=True, configuration=config,
                                           transport=failure(status))
                self.assertEqual(result, {'status': 'http_error', 'results': [], 'error': 'http_error'})
                self.assertNotIn('synthetic-gateway-token', repr(result))

    def test_timeout_and_connection_errors_are_sanitized(self):
        config = web_search.web_search_configuration(environ=self.gateway_env)
        timeout = web_search.search('query', authorized=True, configuration=config,
                                    transport=lambda *_: (_ for _ in ()).throw(TimeoutError()))
        connection = web_search.search('query', authorized=True, configuration=config,
                                       transport=lambda *_: (_ for _ in ()).throw(OSError('private detail')))
        self.assertEqual(timeout, {'status': 'timeout', 'results': [], 'error': 'timeout'})
        self.assertEqual(connection, {'status': 'http_error', 'results': [], 'error': 'http_error'})
        url_timeout = web_search.search('query', authorized=True, configuration=config,
                                        transport=lambda *_: (_ for _ in ()).throw(
                                            urllib.error.URLError(TimeoutError())))
        url_connection = web_search.search('query', authorized=True, configuration=config,
                                           transport=lambda *_: (_ for _ in ()).throw(
                                               urllib.error.URLError('private detail')))
        self.assertEqual(url_timeout['status'], 'timeout')
        self.assertEqual(url_connection['error'], 'http_error')

    def test_response_size_is_bounded(self):
        config = web_search.web_search_configuration(environ=self.gateway_env)

        class OversizedResponse:
            def read(self, size=-1):
                return b'x' * (size + 1)

        result = web_search.search('query', authorized=True, configuration=config,
                                   transport=lambda *_: OversizedResponse())
        self.assertEqual(result['status'], 'http_error')
        self.assertEqual(result['error'], 'response_too_large')

    def test_brave_compatibility_is_preserved(self):
        with patch.dict(os.environ, {'BRAVE_API_KEY': 'synthetic-test-key'}, clear=False):
            config = web_search.web_search_configuration(environ={'BRAVE_API_KEY': 'synthetic-test-key'})
            self.assertEqual(config.provider, 'brave')
            result = web_search.search('synthetic query', authorized=True, configuration=config,
                                       transport=lambda *_: FakeResponse({'web': {'results': [
                                           {'title': 'Brave source', 'url': 'https://example.test/',
                                            'description': 'Synthetic snippet'}]}}))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['results'][0]['source']['provider'], 'brave')


if __name__ == '__main__':
    unittest.main()
