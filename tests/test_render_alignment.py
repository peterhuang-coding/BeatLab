"""The vocal renderer must consume the timing emitted by the composer."""
from pathlib import Path
from types import SimpleNamespace
import sys
import random
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'pipeline'))
import render
import compose
import recipes


class VocalTimingTests(unittest.TestCase):
    def test_vocal_hero_occupies_the_same_grid_as_chopped_hero(self):
        sr = render.SR
        t = np.arange(2*sr)/sr
        phrase = (.25*np.sin(2*np.pi*330*t)).astype('float32')
        hero = dict(id='phrase', asset_id='asset', stem='vocal', start_sec=0, end_sec=16)
        assets = {'asset':dict(id='asset', library_path='fixture.wav', bpm=120, key_note='C')}
        recipe = recipes.build_stem_recipe('timing', hero, [], assets, 92, 1, {'verse':1})
        _, vocals = compose.place_chops(random.Random(1), recipe, hero, [], assets, 'fixture.wav')
        placement = dict(vocals[0], gain=.5)
        self.assertEqual(placement['end_sec']-placement['start_sec'], 2)
        spec = SimpleNamespace(bpm=92, total_bars=1, beat_id='alignment',
                               drum_pattern={}, bass_pattern={},
                               chop_placements=[placement], vocal_placements=[placement])
        with patch.object(render, '_slice_window', return_value=phrase), \
             patch.object(render, '_kit_for_recipe', return_value={}):
            layers = render._render_layers(spec, {}, {}, round(3*sr))
        # Both routes carry the same phrase through the final half-second of
        # the target bar. The old vocal route became silent after 2 seconds.
        tail = slice(round(2.15*sr), round(2.5*sr))
        self.assertGreater(np.sqrt(np.mean(layers['vocal'][0][tail]**2)), .01)
        self.assertLess(np.max(abs(layers['vocal'][0]-layers['chops'][0]*1.5)), 1e-6)

    def test_unstretched_vocal_accents_keep_the_eight_second_limit(self):
        sr = render.SR
        spec = SimpleNamespace(bpm=60, total_bars=3, beat_id='accent',
                               drum_pattern={}, bass_pattern={}, chop_placements=[],
                               vocal_placements=[dict(file='fixture.wav',end_sec=10)])
        with patch.object(render, '_slice_window', return_value=np.ones(10*sr)), \
             patch.object(render, '_kit_for_recipe', return_value={}):
            layers = render._render_layers(spec, {}, {}, 12*sr)
        self.assertEqual(np.count_nonzero(layers['vocal'][0][8*sr:]), 0)
