import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
import common
import ingest
import library

PREFIX='https://s3.amazonaws.com/citizen-dj-assets.labs.loc.gov/audio/samplepacks/loc-jukebox-blues/'


def catalog(extra=''):
    return '''<h3>Rights &amp; access</h3><p>The works in this collection have been identified to be in the public domain and are free to use and reuse without restriction.</p>
    <ul class="preview-list"><li><h4>Old &amp; Blue</h4>
    <a href="http://www.loc.gov/item/jukebox-1/">view item details</a><ul>
    <li>Excerpt starting at 00:34<div><a download href="'''+PREFIX+'''old.wav">download</a>
    <a href="/loc-jukebox-blues/remix/?itemId=jukebox-1&amp;itemStart=35000">remix</a></div></li>'''+extra+'</ul></li></ul>'


class CitizenDJTests(unittest.TestCase):
    def module(self):
        try:
            mod=importlib.import_module('connectors.citizen_dj')
        except ModuleNotFoundError:
            mod=None
        self.assertIsNotNone(mod,'Citizen DJ connector is missing')
        return mod

    def test_catalog_preserves_song_offset_and_deduplicates_links(self):
        mod=self.module()
        records=mod.parse_catalog(catalog('<a download href="'+PREFIX+'old.wav">download</a>'),'blues')
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['title'],'Old & Blue')
        self.assertEqual(records[0]['source_url'],'https://www.loc.gov/item/jukebox-1/')
        self.assertEqual(records[0]['excerpt_start_seconds_label'],34)
        self.assertEqual(records[0]['remix_item_start_ms'],35000)
        self.assertEqual(records[0]['media_url'],PREFIX+'old.wav')

    def test_missing_rights_and_unlisted_hosts_are_rejected(self):
        mod=self.module()
        with self.assertRaises(ValueError):
            mod.parse_catalog(catalog().replace('public domain','unknown'),'blues')
        self.assertEqual(mod.parse_catalog(catalog().replace(PREFIX,'https://example.com/'),'blues'),[])
        with self.assertRaises(ValueError):
            mod.parse_catalog(catalog(),'../../unknown')

    def fixture(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        for obj,key,value in [(common,'ROOT',root),(common,'DB_PATH',root/'db.sqlite'),
                              (library,'LIBRARY',root/'library'),(library,'STAGING',root/'staging')]:
            p=patch.object(obj,key,value);p.start();self.addCleanup(p.stop)
        buf=io.BytesIO();sf.write(buf,np.sin(np.arange(44100*2)*.1)*.1,44100,format='WAV')
        return root,buf.getvalue()

    def test_actual_wav_cache_dedup_and_ingest_provenance(self):
        mod=self.module();root,wav=self.fixture()
        connector=mod.CitizenDJConnector({'cache_root':root/'cache','limit':1,'pause_s':0})
        calls=[]
        def fetch(url,max_bytes):
            calls.append(url)
            return wav if url.endswith('.wav') else catalog().encode()
        with patch.object(connector,'_get',side_effect=fetch):
            entries=connector.scan('blues',set())
            self.assertEqual(len(entries),1);self.assertNotIn('error',entries[0])
            conn=common.get_db();self.addCleanup(conn.close)
            status,_=ingest.ingest_entry(conn,entries[0],'citizen_dj',False)
            self.assertEqual(status,'ok')
            asset=dict(conn.execute('select * from assets').fetchone())
            self.assertEqual(asset['source_url'],'https://www.loc.gov/item/jukebox-1/')
            rights=dict(conn.execute('select * from rights').fetchone())
            self.assertEqual(rights['state'],'allowed')
            self.assertEqual(json.loads(rights['snapshot_json'])['provenance']['excerpt_start_seconds_label'],34)
            self.assertEqual(connector.scan('blues',{entries[0]['md5']}),[])
            self.assertEqual(connector.skipped,1)
            self.assertEqual(sum(u.endswith('.wav') for u in calls),1)
            # An edited cache is not trusted as the previously downloaded source.
            Path(entries[0]['orig_path']).write_bytes(b'corrupt')
            repaired=connector.scan('blues',set())
            self.assertEqual(repaired[0]['md5'],entries[0]['md5'])
            self.assertEqual(sum(u.endswith('.wav') for u in calls),2)

    def test_download_failure_is_reported_and_leaves_no_final_audio(self):
        mod=self.module();root,_=self.fixture()
        connector=mod.CitizenDJConnector({'cache_root':root/'cache','limit':1,'pause_s':0})
        def fetch(url,max_bytes):
            if url.endswith('.wav'):raise OSError('network unavailable')
            return catalog().encode()
        with patch.object(connector,'_get',side_effect=fetch):
            entries=connector.scan('blues',set())
        self.assertEqual(len(entries),1);self.assertIn('network unavailable',entries[0]['error'])
        self.assertEqual(list((root/'cache').rglob('download.wav')),[])

    def test_limit_is_applied_before_audio_downloads(self):
        mod=self.module();root,wav=self.fixture()
        connector=mod.CitizenDJConnector({'cache_root':root/'cache','limit':1,'pause_s':0})
        page=catalog()+catalog().replace('old.wav','second.wav').replace('jukebox-1/','jukebox-2/')
        calls=[]
        def fetch(url,max_bytes):
            calls.append(url)
            return wav if url.endswith('.wav') else page.encode()
        with patch.object(connector,'_get',side_effect=fetch):
            entries=connector.scan('blues',set())
        self.assertEqual(len(entries),1)
        self.assertEqual(sum(url.endswith('.wav') for url in calls),1)
        self.assertTrue(connector.truncated)

    def test_ingest_counts_network_failures(self):
        self.fixture();conn=common.get_db();self.addCleanup(conn.close)
        result=dict(discovered=[],skipped=0,failures=[{'orig_path':'missing','error':'download failed'}])
        with patch.object(ingest.crawler,'crawl',return_value=result):
            stats=ingest.ingest_dir(conn,'blues',source='citizen_dj',limit=1)
        self.assertEqual(stats['fail'],1)


if __name__=='__main__':
    unittest.main()
