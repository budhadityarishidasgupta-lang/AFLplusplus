"""Focused functional coverage for session-mode research authorization."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain_studio as app
from brain_core.conversation_research import ConversationResearchStore
from brain_core.research_coordination import ResearchAuthorizationLedger
from brain_core.provider_config import Provider, build_provider_configuration
from brain_core.research_evidence import validate_search_results


class FreeLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ConversationResearchStore()
        self.session = 'synthetic-session'
        app.st.session_state['_conversation_research_store'] = self.store
        app.st.session_state['_research_authorization_ledger'] = ResearchAuthorizationLedger(
            Path(self.temp.name) / 'ledger.sqlite3')
        app.st.session_state['operating_mode'] = 'FREE_LEARNING'
        self.item = app._prepare_conversation_research(
            'Research public information about fictional Example Observatory schedules', self.session)

    def test_controlled_research_requires_the_existing_approval_state(self):
        app.st.session_state['operating_mode'] = 'CONTROLLED'
        item, _ = self.store.prepare('controlled-session', 'public astronomy sources')
        item.status = 'awaiting_approval'
        calls = []
        with patch.object(app, 'BRAVE_SEARCH_RUNNER', lambda *a, **k: calls.append(a)), \
             patch.object(app, '_research_provider_configuration', return_value=None):
            app.st.session_state['_conversation_research_store'] = self.store
            app._run_authorized_research('controlled-session')
        self.assertEqual(calls, [])
        self.assertEqual(item.status, 'invalid_configuration')

    def test_query_replacement_obeys_pause_and_revokes_old_authorization(self):
        app.st.session_state['_learning_paused_' + self.session] = True
        replacement = app._prepare_conversation_research(
            'Research public information about fictional Example Observatory archives', self.session)
        self.assertEqual(replacement.status, 'awaiting_approval')
        self.assertNotEqual(replacement.authorization_id, self.item.authorization_id)

    def test_free_learning_scoped_search_keeps_provenance(self):
        self.assertEqual(self.item.status, 'authorized')
        config = build_provider_configuration(Provider.SEARXNG, {
            'endpoint': app.web_search.SEARXNG_APPROVED_ENDPOINT,
            'api_token': 'synthetic-only-test-token', 'approved': '1'})
        calls = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size=-1):
                import json
                return json.dumps({'results': [{
                    'title': 'Example Observatory public schedule',
                    'url': 'https://example.test/schedule',
                    'content': 'Synthetic fixture from mocked transport.'}]}).encode()[:size]

        def transport(request, timeout):
            calls.append((request, timeout))
            return Response()

        result = app.web_search.search(self.item.query, authorized=True, configuration=config,
                                       transport=transport)
        self.item.evidence = validate_search_results(result['results'])
        self.item.status = 'completed'
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0].full_url.split('?')[0], app.web_search.SEARXNG_APPROVED_ENDPOINT)
        self.assertEqual(self.item.evidence[0].source_url, 'https://example.test/schedule')
        self.assertFalse(self.item.evidence[0].content_trusted)
        self.assertNotIn('synthetic-only-test-token', repr(self.item.evidence))

    def test_standing_authorization_requires_free_learning_mode(self):
        config = build_provider_configuration(Provider.SEARXNG, {
            'endpoint': app.web_search.SEARXNG_APPROVED_ENDPOINT,
            'api_token': 'synthetic-only-test-token', 'approved': '1'})
        calls = []
        app.st.session_state['operating_mode'] = 'CONTROLLED'
        with patch.object(app, '_research_provider_configuration', return_value=config), \
             patch.object(app, 'BRAVE_SEARCH_RUNNER', lambda *a, **k: calls.append(a)):
            app._run_authorized_research(self.session, standing_authorization=True)
        self.assertEqual(calls, [])
        self.assertEqual(self.item.status, 'awaiting_approval')


if __name__ == '__main__':
    unittest.main()
