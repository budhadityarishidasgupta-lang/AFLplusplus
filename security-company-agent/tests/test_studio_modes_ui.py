"""Minimal Studio mode and pause controls."""
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from streamlit.testing.v1 import AppTest
from brain_core import personality, studio, trainer
from brain_core import model_api, runtime_guard
from brain_studio import installed_models


class StudioModeUITests(unittest.TestCase):
    def test_mode_selector_and_pause_control_are_available(self):
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            profile = root / 'personality.json'
            state = root / 'state.json'
            history = root / 'history.json'
            db = root / 'brain.sqlite3'
            for owner, key, value in (
                    (personality, 'PROFILE_PATH', profile), (trainer, 'STATE_PATH', state),
                    (trainer, 'HISTORY_PATH', history), (studio, 'DATA', root), (studio, 'DB', db)):
                stack.enter_context(patch.object(owner, key, value))
            stack.enter_context(patch.object(model_api, 'local_inventory', return_value=[]))
            stack.enter_context(patch.object(runtime_guard, 'available_memory_gib', return_value=4.0))
            stack.enter_context(patch('brain_studio.installed_models', return_value=[]))
            script = str(Path(__file__).resolve().parents[1] / 'brain_studio.py')
            app = AppTest.from_file(script, default_timeout=20).run()
            self.assertEqual(list(app.exception), [])
            self.assertEqual(next(item for item in app.selectbox if item.label == 'Brain mode').value,
                             'CONTROLLED')
            next(item for item in app.selectbox if item.label == 'Brain mode').set_value('FREE_LEARNING')
            app.run()
            self.assertTrue(any(button.label == 'Pause learning' for button in app.button))
            self.assertTrue(any('Free Learning · active' in item.value for item in app.caption))
            next(button for button in app.button if button.label == 'Pause learning').click()
            app.run()
            self.assertTrue(any('Free Learning · paused' in item.value for item in app.caption))
            self.assertTrue(any(button.label == 'Resume learning' for button in app.button))


if __name__ == '__main__':
    unittest.main()
