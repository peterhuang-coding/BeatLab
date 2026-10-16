import hashlib
import html
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote
from test_ko2_set_page import ko2, base_manifest


class ParentPageChecks(unittest.TestCase):
    def test_feedback_binds_exact_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = json.dumps(base_manifest()).encode()
            (root / 'set.json').write_bytes(raw)
            ko2.generate(root)
            page = (root / 'index.html').read_text()
            uri = re.search(r'href="(data:application/json[^"]+)"', page)[1]
            payload = json.loads(unquote(html.unescape(uri).split(',', 1)[1]))
            self.assertEqual(payload.get('manifest_sha256'), hashlib.sha256(raw).hexdigest())

    @unittest.skipUnless(shutil.which('node'), 'Node required for audio event contract')
    def test_stale_play_rejection_does_not_stop_new_pad(self):
        harness = r'''
const handlers={}, sounds=[];
global.document={addEventListener:(n,f)=>handlers[n]=f};
global.Audio=class {constructor(src){this.src=src;this.handlers={};sounds.push(this)}
pause(){this.paused=true} addEventListener(n,f){this.handlers[n]=f}
play(){return {catch:f=>{this.reject=f}}}};
const pad=()=>({classList:{add(){},remove(){}}});
const click=(name)=>{const p=pad(),b={parentNode:p,getAttribute:()=>name};
handlers.click({target:{closest:()=>b}})};
'''
        harness += ko2.JS
        harness += "click('a.wav');click('b.wav');sounds[0].reject();if(sounds[1].paused)process.exit(2);"
        proc = subprocess.run(['node', '-e', harness], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
