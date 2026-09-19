import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
import requests
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))

PREFIX = 'https://s3.amazonaws.com/citizen-dj-assets.labs.loc.gov/audio/samplepacks/loc-jukebox-blues/'
PAGE = 'https://citizen-dj.labs.loc.gov/loc-jukebox-blues/use/'


def catalog():
    return ('''<p>The works have been identified to be in the public domain and are
    free to use and reuse without restriction.</p><h4>Blue</h4>
    <a href="https://www.loc.gov/item/jukebox-1/">item</a>
    Excerpt starting at 00:34
    <a download href="''' + PREFIX + '''old.wav">wav</a>''')


def wav_bytes(seconds=0.1, bad=False):
    buf = io.BytesIO()
    data = (np.sin(np.arange(int(44100 * seconds)) * .1) * .1).astype('float32')
    sf.write(buf, data, 44100, format='WAV')
    blob = buf.getvalue()
    if bad:
        return b'RIFF' + blob[4:12] + b'not wav' + blob[20:]
    return blob


class Response:
    def __init__(self, body=b'', status=200, headers=None, raise_status=None):
        self.status_code = status
        self.headers = headers or {}
        self.body = body
        self.raise_status = raise_status

    def raise_for_status(self):
        if self.raise_status is not None:
            raise requests.HTTPError(str(self.raise_status))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]


class CitizenDJTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mod = importlib.import_module('connectors.citizen_dj')
        self.connector = self.mod.CitizenDJConnector({'cache_root': self.root / 'cache',
                                                       'pause_s': 0})

    def test_retries_transient_5xx_then_succeeds(self):
        wav = wav_bytes()
        responses = [Response(status=500, raise_status=500),
                     Response(status=599, raise_status=599), Response(wav)]

        def get(url, **kwargs):
            return responses.pop(0)

        sleeps = []
        with patch.object(self.mod.requests, 'get', side_effect=get), \
                patch.object(self.mod.time, 'sleep', sleeps.append):
            blob = self.connector._get(PREFIX + 'old.wav', len(wav))
        self.assertEqual(blob, wav)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_permanent_failure_is_not_retried(self):
        calls = []

        def get(url, **kwargs):
            calls.append(url)
            return Response(status=403, raise_status=403)

        with patch.object(self.mod.requests, 'get', side_effect=get):
            with self.assertRaises(requests.HTTPError):
                self.connector._get(PREFIX + 'old.wav', 10)
        self.assertEqual(len(calls), 1)

    def test_connection_retries_are_bounded_and_429_can_recover(self):
        with patch.object(self.mod.requests, 'get', side_effect=requests.ConnectionError('offline')) as get, \
                patch.object(self.mod.time, 'sleep'):
            with self.assertRaises(requests.ConnectionError):
                self.connector._get(PAGE, 100)
        self.assertEqual(get.call_count, 3)
        with patch.object(self.mod.requests, 'get', side_effect=[Response(status=429), Response(b'ok')]) as get, \
                patch.object(self.mod.time, 'sleep'):
            self.assertEqual(self.connector._get(PAGE, 100), b'ok')
        self.assertEqual(get.call_count, 2)

    def test_deadline_expires_before_request(self):
        self.connector.deadline = time.monotonic() - 1
        with patch.object(self.mod.requests, 'get') as get:
            with self.assertRaises(TimeoutError):
                self.connector._get(PAGE, 10)
        get.assert_not_called()

    def test_advertised_and_actual_size_limits_enforced(self):
        body = b'abcdef'
        with patch.object(self.mod.requests, 'get',
                          return_value=Response(body, headers={'Content-Length': '99'})):
            with self.assertRaises(ValueError):
                self.connector._get(PAGE, 5)
        with patch.object(self.mod.requests, 'get',
                          return_value=Response(body, headers={})):
            with self.assertRaises(ValueError):
                self.connector._get(PAGE, 5)

    def test_rejects_invalid_urls(self):
        bad = [
            'http://citizen-dj.labs.loc.gov/loc-jukebox-blues/use/',
            'https://citizen-dj.labs.loc.gov/loc-jukebox-blues/use/?x=1',
            'https://citizen-dj.labs.loc.gov/loc-jukebox-blues/use/#frag',
            'https://citizen-dj.labs.loc.gov/loc-jukebox-blues/use/../secret',
            'https://user:pass@citizen-dj.labs.loc.gov/loc-jukebox-blues/use/',
            'https://citizen-dj.labs.loc.gov:443/loc-jukebox-blues/use/',
            'https://example.com/loc-jukebox-blues/use/',
            'https://s3.amazonaws.com/citizen-dj-assets.labs.loc.gov/audio/samplepacks/loc-jukebox-blues/../x.wav',
            'https://s3.amazonaws.com/other/x.wav',
        ]
        for url in bad:
            with self.assertRaises(ValueError):
                self.connector._validate_url(url)
        self.connector._validate_url(PAGE)
        self.connector._validate_url(PREFIX + 'old.wav')

    def test_discover_fetch_cache_and_rights_validation(self):
        html = catalog().encode()
        wav = wav_bytes()

        def get(url, **kwargs):
            body = html if url == PAGE else wav
            return Response(body, headers={'Content-Length': str(len(body))})

        with patch.object(self.mod.requests, 'get', side_effect=get):
            cat = self.connector.discover('blues')
            entry = self.connector.fetch_entry('blues', cat['records'][0], cat)
        self.assertEqual(entry['rights']['state'], 'allowed')
        self.assertTrue(Path(cat['catalog_snapshot']).is_file())
        provenance = self.connector.cached_entry(cat['records'][0])
        self.assertIsNotNone(provenance)
        self.assertEqual(provenance['media_url'], PREFIX + 'old.wav')
        self.assertNotIn('metadata_sha256', provenance)

        with patch.object(self.connector, '_get') as getter:
            again = self.connector.fetch_entry('blues', cat['records'][0], cat)
        getter.assert_not_called()
        self.assertEqual(again['provenance']['retrieved_at'], entry['provenance']['retrieved_at'])

        snapshot = Path(cat['catalog_snapshot'])
        snapshot.write_bytes(snapshot.read_bytes().replace(b'public domain', b'unknown domain'))
        with self.assertRaises(ValueError):
            self.connector.fetch_entry('blues', cat['records'][0], cat)

    def test_changed_catalog_snapshot_reuses_verified_audio(self):
        first_html = catalog().encode()
        second_html = catalog().replace('Blue</h4>', 'Blue Updated</h4>').encode()
        wav = wav_bytes()
        bodies = iter([first_html, wav, second_html])

        def get(url, **kwargs):
            return Response(next(bodies), headers={})

        with patch.object(self.mod.requests, 'get', side_effect=get):
            first = self.connector.discover('blues')
            entry = self.connector.fetch_entry('blues', first['records'][0], first)
            second = self.connector.discover('blues')

        self.assertNotEqual(first['catalog_sha256'], second['catalog_sha256'])
        meta = Path(entry['provenance']['catalog_snapshot']).parent / \
            hashlib.sha256((PREFIX + 'old.wav').encode()).hexdigest()[:20] / 'source.json'
        before = meta.read_text()
        with patch.object(self.connector, '_get') as getter:
            again = self.connector.fetch_entry('blues', second['records'][0], second)
        getter.assert_not_called()
        self.assertEqual(again['provenance']['retrieved_at'],
                         entry['provenance']['retrieved_at'])
        self.assertEqual(again['provenance']['catalog_sha256'], second['catalog_sha256'])
        self.assertNotEqual(before, meta.read_text())

    def test_invalid_cache_reacquires(self):
        html = catalog().encode()
        cat = self._catalog_from_html(html)
        with patch.object(self.connector, '_get', return_value=wav_bytes()):
            good = self.connector.fetch_entry('blues', cat['records'][0], cat)
        meta = Path(good['orig_path']).with_name('source.json')
        data = json.loads(meta.read_text())
        data['download_sha256'] = 'x' * 64
        meta.write_text(json.dumps(data))
        with patch.object(self.connector, '_get', return_value=wav_bytes()) as getter:
            self.connector.fetch_entry('blues', cat['records'][0], cat)
        getter.assert_called_once()

    def test_full_audio_is_decoded_and_nonfinite_floats_rejected(self):
        path = self.root / 'nan.wav'
        data = np.array([0.0, np.nan], dtype='float32')
        sf.write(path, data, 44100, format='WAV', subtype='FLOAT')
        with self.assertRaises(ValueError):
            self.connector._valid_audio(path)

        good = self.root / 'good.wav'
        sf.write(good, np.zeros(8, dtype='float32'), 44100, format='WAV')
        self.assertEqual(self.connector._valid_audio(good).frames, 8)

    def test_invalid_audio_not_published(self):
        html = catalog().encode()
        snapshot = self.root / 'cache' / 'blues'
        snapshot.mkdir(parents=True)
        page = snapshot / ('catalog-' + '0' * 16 + '.html')
        page.write_bytes(html)
        cat = self._catalog_from_html(html, page)
        with patch.object(self.connector, '_get', return_value=wav_bytes(bad=True)):
            with self.assertRaises(Exception):
                self.connector.fetch_entry('blues', cat['records'][0], cat)
        self.assertEqual(list(snapshot.rglob('download.wav')), [])
        self.assertEqual(list(snapshot.rglob('*.part')), [])

    def _catalog_from_html(self, html, page=None):
        if page is None:
            snapshot = self.root / 'cache' / 'blues'
            snapshot.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(html).hexdigest()[:16]
            page = snapshot / f'catalog-{digest}.html'
            page.write_bytes(html)
        return {'collection': 'blues', 'collection_url': PAGE,
                'catalog_sha256': hashlib.sha256(html).hexdigest(),
                'catalog_snapshot': str(page),
                'records': self.mod._ordered(self.mod.parse_catalog(html.decode(), 'blues'))}


if __name__ == '__main__':
    unittest.main()
