"""Relocation regressions; use disposable data, never the desktop library."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
PIPE = PROJECT / "pipeline"


class PortabilityTests(unittest.TestCase):
    def paths(self, **overrides):
        env = dict(os.environ, PYTHONPATH=str(PIPE))
        for key in ("BEATLAB_ROOT", "BEATLAB_MIRROR_ROOT"):
            env.pop(key, None)
        env.update(overrides)
        result = subprocess.run(
            [sys.executable, "-c", "import common,json; print(json.dumps([str(common.ROOT),str(common.MIRROR_ROOT),str(common.PIPELINE)]))"],
            env=env, cwd=tempfile.gettempdir(), check=True, text=True, capture_output=True,
        )
        return [Path(p) for p in json.loads(result.stdout)]

    def test_default_paths_follow_checkout_from_another_cwd(self):
        self.assertEqual(self.paths(), [PROJECT, PROJECT / "exports", PIPE])

    def test_data_override_keeps_code_in_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "audio data"
            self.assertEqual(self.paths(BEATLAB_ROOT=str(root)), [root, root / "exports", PIPE])

    def test_paths_expand_home_and_allow_separate_exports(self):
        paths = self.paths(BEATLAB_ROOT="~/BeatLab-test-data", BEATLAB_MIRROR_ROOT="~/BeatLab-test-exports")
        self.assertEqual(paths, [Path.home() / "BeatLab-test-data", Path.home() / "BeatLab-test-exports", PIPE])

    def test_delegation_preserves_flags_before_positionals(self):
        sys.path.insert(0, str(PIPE))
        try:
            spec = importlib.util.spec_from_file_location("beatlab_cli", PIPE / "pipeline.py")
            cli = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cli)
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                received = folder / "received.json"
                (folder / "separate.py").write_text(
                    "import json,sys\nfrom pathlib import Path\n"
                    "Path(__file__).with_name('received.json').write_text(json.dumps(sys.argv[1:]))\n"
                )
                with patch.object(cli, "PIPE", folder), patch.object(cli, "PY", Path(sys.executable)), \
                     patch.object(sys, "argv", ["pipeline.py", "separate", "--all", "--skip-if-done"]):
                    self.assertEqual(cli.main(), 0)
                self.assertEqual(json.loads(received.read_text()), ["--all", "--skip-if-done"])
        finally:
            sys.path.remove(str(PIPE))

    def test_ingest_forwards_source_and_timeout_with_positional_collection(self):
        sys.path.insert(0,str(PIPE))
        self.addCleanup(sys.path.remove,str(PIPE))
        spec=importlib.util.spec_from_file_location('beatlab_ingest_cli',PIPE/'pipeline.py')
        cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        with patch.object(cli,'run') as run, patch.object(sys,'argv',
                ['beatlab','ingest','--source','citizen_dj','--timeout','15','blues']):
            self.assertEqual(cli.main(),0)
        self.assertEqual(run.call_args.args,('ingest.py','--source','citizen_dj','--timeout','15.0','blues'))

    def test_crate_command_preserves_batch_and_resume_options(self):
        sys.path.insert(0, str(PIPE))
        self.addCleanup(sys.path.remove, str(PIPE))
        spec = importlib.util.spec_from_file_location('beatlab_crate_cli', PIPE / 'pipeline.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with patch.object(cli, 'run') as run, patch.object(sys, 'argv',
                ['beatlab', 'crate', '--batch-id', 'fixed-batch', '--resume']):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(run.call_args.args, ('crate.py', '--batch-id', 'fixed-batch', '--resume'))


if __name__ == "__main__":
    unittest.main()
