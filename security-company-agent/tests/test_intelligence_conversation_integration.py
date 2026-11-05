"""Offline integration tests for Conversation's existing intelligence pipeline."""
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from brain_core.providers import InferenceResult, ProviderID
from brain_core.research_evidence import ResearchEvidence
import brain_studio as app_module


class ConversationIntelligenceIntegrationTests(unittest.TestCase):
    PRINCIPLE = 'Always verify claims before deciding to avoid unsupported conclusions.'

    def setUp(self):
        from test_studio_ui import StudioUITests
        self.fixture = StudioUITests(methodName='test_ui_readiness_inspection_does_not_initialize_runtime_files')
        self.fixture.setUp()
        self.app = self.fixture.app.run()
        self.session_id = self.app.session_state['conversation_id']
        session_state_type = type('SessionStateFixture', (dict,), {'get': dict.get})
        self.session_state = session_state_type(dict(self.app.session_state))
        self.research = Mock(status='completed', evidence=(ResearchEvidence(
            claim=self.PRINCIPLE, snippet=self.PRINCIPLE, title='Synthetic source',
            source_url='https://example.test/evidence', retrieved_at='2026-01-01T00:00:00Z',
            confidence=0.8, content_trusted=False, uncertainty='Unverified synthetic web claim.'),))

    def inference(self, provider=ProviderID.OPENAI_CLOUD, *, status='success', structured=None,
                  request_id='request-synthetic'):
        if structured is None:
            structured = {'claims': [self.PRINCIPLE]}
        return InferenceResult(provider, 'synthetic-model', json.dumps(structured), structured,
                               status, None if status == 'success' else 'timeout', .01,
                               None, None, request_id, None, True,
                               provider is not ProviderID.LOCAL_OLLAMA)

    def test_multisource_result_is_qualified_and_candidate_uses_review_ui_state(self):
        result = self.inference()
        with patch.object(app_module.st, 'session_state', self.session_state):
            response = app_module._present_intelligence_result(self.session_id, result, self.research,
                                                                response='Original answer')
            state = self.session_state['_candidate_learning_review']
        self.assertIn('Qualified summary of available source claims', response)
        self.assertIn('https://example.test/evidence', response)
        self.assertIn('Uncertainty:', response)
        self.assertIn('validated_model_content_not_factually_verified', response)
        self.assertIn('unverified_web_evidence', response)
        self.assertNotEqual(response, 'Original answer')
        self.assertEqual(state['status'], 'candidate_review')
        self.assertEqual(state['candidate'].proposed_principle, self.PRINCIPLE.rstrip('.'))
        self.assertEqual(state['candidate'].review_status, 'pending_review')
        self.assertEqual(state['conversation_id'], self.session_id)
        with patch.object(app_module.st, 'session_state', self.session_state):
            app_module._render_candidate_learning_review(self.session_id)
        reviewed = self.session_state.get('_candidate_learning_review')
        self.assertEqual(reviewed['status'], 'candidate_review')
        self.assertTrue(reviewed['candidate'])

    def test_single_source_falls_back_without_candidate_or_synthetic_response(self):
        result = self.inference(provider=ProviderID.ALIBABA_QWEN_CLOUD)
        with patch.object(app_module.st, 'session_state', self.session_state):
            response = app_module._present_intelligence_result(self.session_id, result, None,
                                                                response='Provider answer')
        self.assertEqual(response, 'Provider answer')
        with patch.object(app_module.st, 'session_state', self.session_state):
            empty_claim_response = app_module._present_intelligence_result(
                self.session_id, result, None, response='Keep this response', claims=[])
        self.assertEqual(empty_claim_response, 'Keep this response')
        self.assertNotIn('Sources:', response)
        self.assertNotIn('_candidate_learning_review', self.session_state)

    def test_failed_provider_and_absent_research_do_not_create_candidate(self):
        with patch.object(app_module.st, 'session_state', self.session_state):
            app_module._present_intelligence_result(self.session_id,
                self.inference(status='failed'), self.research)
        self.assertNotIn('_candidate_learning_review', self.session_state)
        with patch.object(app_module.st, 'session_state', self.session_state):
            response = app_module._present_intelligence_result(self.session_id,
                self.inference(provider=ProviderID.ALIBABA_QWEN_CLOUD), None,
                response='Valid single-source Qwen answer')
        self.assertEqual(response, 'Valid single-source Qwen answer')
        self.assertNotIn('_candidate_learning_review', self.session_state)

    def test_research_must_be_completed_before_it_can_contribute(self):
        pending = Mock(status='awaiting_approval', evidence=self.research.evidence)
        result = self.inference()
        with patch.object(app_module.st, 'session_state', self.session_state):
            response = app_module._present_intelligence_result(
                self.session_id, result, pending, response='Unchanged provider answer')
        self.assertEqual(response, 'Unchanged provider answer')
        self.assertNotIn('_candidate_learning_review', self.session_state)
        approved_empty = Mock(status='completed', evidence=())
        with patch.object(app_module.st, 'session_state', self.session_state):
            response = app_module._present_intelligence_result(
                self.session_id, result, approved_empty, response='Single provider answer')
        self.assertEqual(response, 'Single provider answer')

    def test_candidate_id_is_stable_for_reruns_and_distinct_for_different_lessons(self):
        first_result = self.inference(provider=ProviderID.ALIBABA_QWEN_CLOUD,
                                      request_id='request-first')
        with patch.object(app_module.st, 'session_state', self.session_state):
            first = app_module._offer_candidate_from_intelligence(
                self.session_id, first_result, self.research)
            rerun = app_module._offer_candidate_from_intelligence(
                self.session_id, first_result, self.research)
        self.assertIs(first, rerun)
        self.assertEqual(first['candidate_id'], rerun['candidate_id'])

        rerun_with_new_request_id = self.inference(provider=ProviderID.ALIBABA_QWEN_CLOUD,
                                                   request_id='request-rerun')
        with patch.object(app_module.st, 'session_state', self.session_state):
            same_lesson = app_module._offer_candidate_from_intelligence(
                self.session_id, rerun_with_new_request_id, self.research)
        self.assertEqual(first['candidate_id'], same_lesson['candidate_id'])

        different_principle = 'Never decide before you verify uncertain claims with evidence.'
        different_result = self.inference(
            provider=ProviderID.ALIBABA_QWEN_CLOUD,
            request_id='request-distinct', structured={'claims': [different_principle]})
        different_research = Mock(status='completed', evidence=(ResearchEvidence(
            claim=different_principle, snippet=different_principle, title='Second synthetic source',
            source_url='https://example.test/second', retrieved_at='2026-01-02T00:00:00Z',
            confidence=0.8, content_trusted=False, uncertainty='Unverified synthetic web claim.'),))
        with patch.object(app_module.st, 'session_state', self.session_state):
            distinct = app_module._offer_candidate_from_intelligence(
                self.session_id, different_result, different_research)
        self.assertNotEqual(first['candidate_id'], distinct['candidate_id'])
        self.assertNotEqual(first['candidate'].proposed_principle, distinct['candidate'].proposed_principle)

    def test_duplicate_rerun_after_approval_does_not_create_another_approval(self):
        result = self.inference(provider=ProviderID.ALIBABA_QWEN_CLOUD, request_id='request-before-approval')
        with patch.object(app_module.st, 'session_state', self.session_state):
            state = app_module._offer_candidate_from_intelligence(self.session_id, result, self.research)
        state['text_reviewed'] = True
        self.session_state[f"candidate_confirm_{state['candidate_id']}"] = True
        rerun_result = self.inference(provider=ProviderID.ALIBABA_QWEN_CLOUD,
                                      request_id='request-after-approval')
        with patch.object(app_module.st, 'session_state', self.session_state):
            rerun = app_module._offer_candidate_from_intelligence(
                self.session_id, rerun_result, self.research)
        self.assertIs(rerun, state)
        self.assertEqual(rerun['status'], 'candidate_review')
        self.assertEqual(rerun['candidate_id'], state['candidate_id'])

    def tearDown(self):
        self.fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
