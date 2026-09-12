"""Exercise the Review service against the real SQLite schema and run layout."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import common
import feedback


class FeedbackIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in (("ROOT", self.root), ("DB_PATH", self.root / "db.sqlite")):
            p = patch.object(common, name, value)
            p.start()
            self.addCleanup(p.stop)
        for kind in ("loop", "chop", "stem"):
            folder = self.root / "beats" / "test-run" / kind
            folder.mkdir(parents=True)
            (folder / "recipe.json").write_text(json.dumps({"kind": kind, "hero": {"asset_id": "fixture-asset"}}))
            (folder / "provenance.json").write_text(json.dumps({"hero": {"source": "generated-fixture"}}))
            (folder / "chops").mkdir()
            (folder / "chops" / "chop.wav").write_bytes(b"test-chop-payload")
        self.server = feedback.FeedbackServer(port=0)
        self.thread = threading.Thread(target=self.server.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)

    def request(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(f"http://127.0.0.1:{self.server.port}{path}", data=data,
                      headers={"Content-Type": "application/json"})
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_index_discovers_current_render_layout(self):
        self.assertEqual(feedback.list_targets(), [("test-run", "run")])

    def test_read_real_schema_feedback(self):
        conn = common.get_db()
        try:
            common.upsert_feedback(conn, {"id": "test-run:loop", "run_id": "test-run",
                "candidate_id": "loop", "dims": {"drums": 4}, "reasons": ["worth_keeping"], "verdict": "rated"})
        finally:
            conn.close()
        status, body = self.request('/api/feedback?run_id=test-run&candidate_id=loop')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['rows'][0]['dims'], {"drums": 4})
        self.assertEqual(body['rows'][0]['reasons'], ["worth_keeping"])

    def test_keep_rate_and_reload_preserve_outcome(self):
        payload = {"run_id": "test-run", "candidate_id": "loop"}
        status, body = self.request('/api/keep', payload)
        self.assertEqual(status, 200, body)
        status, body = self.request('/api/feedback', dict(payload, verdict="rated", dims={"drums": 4}))
        self.assertEqual(status, 200, body)
        status, body = self.request('/api/feedback?run_id=test-run&candidate_id=loop')
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body['rows']), 1)
        self.assertEqual(body['rows'][0]['ableton_outcome'], "kept")
        self.assertEqual(body['rows'][0]['dims'], {"drums": 4})

    def test_export_preserves_recipe_provenance_and_chops(self):
        status, body = self.request('/api/export', {"run_id": "test-run", "candidate_id": "loop"})
        self.assertEqual(status, 200, body)
        output = self.root / 'exports/test-run/loop'
        self.assertEqual(json.loads((output / 'recipe.json').read_text())['kind'], 'loop')
        self.assertEqual(json.loads((output / 'provenance.json').read_text())['hero']['source'], 'generated-fixture')
        self.assertEqual((output / 'chops/chop.wav').read_bytes(), b'test-chop-payload')


if __name__ == '__main__':
    unittest.main()
