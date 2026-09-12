import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import common
import recipes


class RecipeStateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for key, val in [('ROOT', Path(tmp.name)), ('DB_PATH', Path(tmp.name) / 'db.sqlite')]:
            p = patch.object(common, key, val)
            p.start()
            self.addCleanup(p.stop)
        self.conn = common.get_db()
        self.addCleanup(self.conn.close)

    def test_completed_job_is_visible_to_composer(self):
        common.upsert_job(self.conn, {'id': 'run1', 'type': 'run', 'state': 'generated'})
        self.assertEqual(recipes.job_status('run1'), 'generated')
        self.assertIsNone(recipes.job_status('unknown'))

    def test_review_feedback_reaches_next_composition(self):
        common.upsert_feedback(self.conn, {'id': 'run1:chop', 'run_id': 'run1',
            'candidate_id': 'chop', 'verdict': 'reject', 'dims': {'chop': 2}, 'reasons': ['chop_fragmented']})
        rows = recipes.get_feedback_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['reasons'], ['chop_fragmented'])

    def test_ui_reason_codes_change_recipe_prior(self):
        baseline = recipes.recipe_prior([])
        after = recipes.recipe_prior([{'reasons': ['chop_fragmented'], 'dims': {'chop': 2}}])
        self.assertLess(after['chop'], baseline['chop'])
        self.assertAlmostEqual(sum(after.values()), 1.0)

    def test_source_moment_recipe_references_an_existing_source(self):
        source = common.ROOT / 'library/loops/fixture/source.wav'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'fixture')
        hero = {'id': 'moment1', 'asset_id': 'fixture', 'stem': 'source', 'start_sec': 0, 'end_sec': 2}
        assets = {'fixture': {'id': 'fixture', 'library_path': str(source), 'bpm': 92, 'key_note': 'C'}}
        manifest = recipes.build_stem_recipe('run1', hero, [], assets, 92, 1, {'intro': 2})
        self.assertTrue(Path(manifest['chops'][0]['file']).is_file(), manifest['chops'][0])


if __name__ == '__main__':
    unittest.main()
