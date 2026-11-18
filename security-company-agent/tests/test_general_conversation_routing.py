"""Mocked end-to-end tests for general versus emotional Conversation routing."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain_studio as app_module
from brain_core import conversation, lessons, studio


class GeneralConversationRoutingTests(unittest.TestCase):
    def setUp(self):
        from test_studio_ui import StudioUITests
        self.fixture = StudioUITests(
            methodName='test_ui_readiness_inspection_does_not_initialize_runtime_files')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.app = self.fixture.app.run()
        self.cid = self.app.session_state['conversation_id']
        self.profile = {'identity_name': 'Synthetic', 'identity_id': 'synthetic', 'mission': 'offline test'}
        self.data_dir = self.fixture.paths['data_dir']
        self.db_path = self.fixture.paths['db_path']
        self.data_patch = patch.object(studio, 'DATA', self.data_dir)
        self.db_patch = patch.object(studio, 'DB', self.db_path)
        self.data_patch.start()
        self.db_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.addCleanup(self.db_patch.stop)
        self.session_state = app_module.st.session_state

    def _dispatch(self, text, *, inference='Automatic (Local first)'):
        state = app_module.st.session_state
        state['conversation_id'] = self.cid
        state['studio_inference'] = inference
        history = conversation.load_messages(
            self.cid, data_dir=self.fixture.paths['data_dir'], db_path=self.fixture.paths['db_path'])
        with patch.object(app_module, '_start_automatic_journey') as automatic, \
             patch.object(app_module, 'trainer') as trainer, \
             patch.object(app_module, '_prepare_conversation_research') as prepare_research, \
             patch.object(app_module, '_run_general_answer') as general_answer:
            trainer.infer.return_value = {'narrative': 'mocked', 'conversational_response': 'mocked'}
            app_module._handle_message(text, self.cid, history, self.profile,
                                       Mock(local=True, model='offline-test'), None)
        return automatic, trainer.infer, prepare_research, general_answer

    def test_sky_question_uses_general_answer_not_automatic_emotional_pipeline(self):
        text = 'Explain why the sky is blue in two sentences.'
        automatic, inference, research, general_answer = self._dispatch(text)
        self.assertEqual(conversation.classify_intent(text).intent, 'general')
        automatic.assert_not_called()
        self.assertEqual(general_answer.call_args.args[:2], (text, self.cid))
        inference.assert_not_called()
        research.assert_not_called()
        rows = conversation.load_messages(self.cid, data_dir=self.fixture.paths['data_dir'],
                                          db_path=self.fixture.paths['db_path'])
        self.assertEqual(rows[-1]['role'], 'user')

    def test_interpersonal_question_uses_automatic_emotional_pipeline(self):
        text = "My friend hasn't replied for two days. Does that mean they no longer care?"
        automatic, inference, research, _ = self._dispatch(text)
        self.assertEqual(conversation.classify_intent(text).intent, 'emotional_reasoning')
        automatic.assert_called_once_with(text, self.cid, self.profile, unittest.mock.ANY)
        inference.assert_not_called()
        research.assert_not_called()

    def test_latest_developments_uses_general_answer_and_research_preview(self):
        text = 'What are the latest developments in AI agents?'
        automatic, inference, research, general_answer = self._dispatch(text)
        self.assertEqual(conversation.classify_intent(text).intent, 'general')
        automatic.assert_not_called()
        self.assertEqual(general_answer.call_args.args[:2], (text, self.cid))
        inference.assert_not_called()
        research.assert_not_called()
        general_answer.assert_called_once()
        self.assertEqual(conversation.load_messages(
            self.cid, data_dir=self.fixture.paths['data_dir'], db_path=self.fixture.paths['db_path'])[-1]['role'],
            'user')

    def test_incomplete_lesson_can_resume_and_be_clarified_without_capturing_other_intents(self):
        self._dispatch('I want to teach you something.')
        draft = conversation.load_lesson_state(self.cid, data_dir=self.data_dir, db_path=self.db_path)
        self.assertEqual(draft['status'], 'incomplete')
        app_module.st.session_state['pending_lesson'] = draft
        history = conversation.load_messages(self.cid, data_dir=self.data_dir, db_path=self.db_path)
        with patch.object(app_module.st, 'session_state', self.session_state):
            app_module._handle_message('Continue the training draft.', self.cid, history, self.profile,
                                       Mock(local=True, model='offline-test'), None)
        self.assertEqual(conversation.load_lesson_state(
            self.cid, data_dir=self.data_dir, db_path=self.db_path)['status'], 'incomplete')
        app_module.st.session_state['pending_lesson'] = draft
        history = conversation.load_messages(self.cid, data_dir=self.data_dir, db_path=self.db_path)
        clarification = 'Scenario summary: A stressful conversation.\nEmotional context: Concern.'
        with patch.object(app_module.st, 'session_state', self.session_state):
            app_module._handle_message(clarification, self.cid, history, self.profile,
                                       Mock(local=True, model='offline-test'), None)
        updated = conversation.load_lesson_state(self.cid, data_dir=self.data_dir, db_path=self.db_path)
        self.assertIn('scenario_summary', updated['fields'])
        self.assertIn('emotional_context', updated['fields'])
        self.assertEqual(updated['status'], 'incomplete')

    def test_explicit_training_request_persists_draft_then_approval_saves_retrievable_lesson(self):
        text = 'I want to teach you that silence can have many explanations.'
        self._dispatch(text)
        draft = conversation.load_lesson_state(self.cid, data_dir=self.data_dir, db_path=self.db_path)
        self.assertEqual(draft['status'], 'incomplete')
        self.assertEqual(conversation.classify_intent(text).intent, 'training')

        structured = (
            'Scenario summary: A friend replies less often.\n'
            'Emotional context: I feel uncertain.\n'
            'Observed facts: Replies are less frequent.\n'
            'Assumptions or uncertainties: The reason is unknown.\n'
            'Corrective principle: Do not treat silence as proof of rejection.\n'
            'Recommended response pattern: Ask calmly and respectfully.')
        history = conversation.load_messages(
            self.cid, data_dir=self.data_dir, db_path=self.db_path)
        with patch.object(app_module.st, 'session_state', self.session_state):
            app_module.st.session_state['pending_lesson'] = draft
            app_module._handle_message(structured, self.cid, history, self.profile,
                                       Mock(local=True, model='offline-test'), None)
        proposal = conversation.load_lesson_state(self.cid, data_dir=self.data_dir, db_path=self.db_path)
        self.assertEqual(proposal['status'], 'proposed')
        self.assertEqual(proposal['record']['corrective_principle'], 'Do not treat silence as proof of rejection.')
        self.assertEqual(lessons.list_lessons(data_dir=self.data_dir, db_path=self.db_path), [])

        self.session_state['pending_lesson'] = proposal
        history = conversation.load_messages(self.cid, data_dir=self.data_dir, db_path=self.db_path)
        with patch.object(app_module.st, 'session_state', self.session_state):
            app_module._handle_message('Approve this lesson', self.cid, history, self.profile,
                                       Mock(local=True, model='offline-test'), None)
        self.assertIsNone(conversation.load_lesson_state(self.cid, data_dir=self.data_dir, db_path=self.db_path))
        approved = lessons.retrieve('silence rejection', data_dir=self.data_dir, db_path=self.db_path)
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]['corrective_principle'], 'Do not treat silence as proof of rejection.')

        rows = conversation.load_messages(self.cid, data_dir=self.data_dir, db_path=self.db_path)
        self.assertIn('lesson_approved', [item['message_type'] for item in rows])


if __name__ == '__main__':
    unittest.main()
