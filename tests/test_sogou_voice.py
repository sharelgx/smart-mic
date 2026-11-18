import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
import sogou_voice as voice


class SogouAudioOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_requires_idle_then_stable_active_input(self):
        owner = frozenset({123})
        with patch.object(voice, '_recorders', side_effect=[frozenset(), owner, owner]), \
             patch.object(voice, '_launch', new_callable=AsyncMock) as launch:
            self.assertEqual(await voice.start_session_voice(), owner)
            launch.assert_awaited_once_with('toolbox')
        with patch.object(voice, '_recorders', return_value=owner), \
             patch.object(voice, '_launch', new_callable=AsyncMock) as launch:
            with self.assertRaises(RuntimeError):
                await voice.start_session_voice()
            launch.assert_not_awaited()

    async def test_end_requires_same_active_pids_and_release(self):
        owner = frozenset({123})
        with patch.object(voice, '_recorders', side_effect=[owner, owner] + [frozenset()] * 3), \
             patch.object(voice, '_launch', new_callable=AsyncMock) as launch:
            await voice.finish_voice(owner)
            launch.assert_awaited_once_with('hotkey')
        with patch.object(voice, '_recorders', return_value=frozenset({789})), \
             patch.object(voice, '_launch', new_callable=AsyncMock) as launch:
            with self.assertRaises(RuntimeError):
                await voice.finish_voice(owner)
            launch.assert_not_awaited()
