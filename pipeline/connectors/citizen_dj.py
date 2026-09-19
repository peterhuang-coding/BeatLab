"""Discover historical musical excerpts from Citizen DJ's public download pages."""
from collections import defaultdict
import contextlib
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlparse

import numpy as np
import requests
import soundfile as sf

import common
from connectors import register

SITE = 'https://citizen-dj.labs.loc.gov'
COLLECTIONS = {'blues': 'loc-jukebox-blues', 'jazz': 'loc-jukebox-jazz'}
ASSETS = 'https://s3.amazonaws.com/citizen-dj-assets.labs.loc.gov/audio/samplepacks/'
CREDIT = 'Citizen DJ Project, Library of Congress, National Jukebox.'
RIGHTS_REQUIRED = ('identified to be in the public domain',
                   'free to use and reuse without restriction')


class _CatalogParser(HTMLParser):
    def __init__(self, prefix):
        super().__init__()
        self.prefix = prefix
        self.records = []
        self.seen = set()
        self.in_title = False
        self.title = ''
        self.item = ''
        self.start = None
        self.last = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        href = attrs.get('href', '')
        if tag == 'h4':
            self.in_title = True
            self.title = ''
            self.item = ''
            self.start = None
        if tag != 'a':
            return
        parsed = urlparse(href)
        if parsed.hostname == 'www.loc.gov' and parsed.path.startswith('/item/'):
            self.item = 'https://www.loc.gov' + parsed.path
        if 'download' in attrs and href.startswith(self.prefix) and href.endswith('.wav'):
            if '..' in parsed.path.split('/') or href in self.seen:
                return
            if not self.item or self.start is None:
                return
            self.last = dict(title=self.title.strip(), source_url=self.item, media_url=href,
                             excerpt_start_seconds_label=self.start)
            self.records.append(self.last)
            self.seen.add(href)
        if self.last is not None and parsed.path.endswith('/remix/'):
            value = parse_qs(parsed.query).get('itemStart', [''])[0]
            if value.isdigit():
                self.last['remix_item_start_ms'] = int(value)

    def handle_endtag(self, tag):
        if tag == 'h4':
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        match = re.search(r'Excerpt starting at (\d+):(\d{2})(?::(\d{2}))?', data)
        if match:
            a, b, c = match.groups()
            self.start = int(a) * 60 + int(b) if c is None else int(a) * 3600 + int(b) * 60 + int(c)


def parse_catalog(html, collection):
    if collection not in COLLECTIONS:
        raise ValueError('Supported collections: blues, jazz')
    if any(text not in html for text in RIGHTS_REQUIRED):
        raise ValueError('Collection rights statement is absent or has changed')
    parser = _CatalogParser(ASSETS + COLLECTIONS[collection] + '/')
    parser.feed(html)
    return parser.records


def _sha_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _sha_bytes(blob):
    return hashlib.sha256(blob).hexdigest()


def _atomic_bytes(path, blob):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        tmp.write_bytes(blob)
        tmp.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _atomic_json(path, data):
    encoded = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
    _atomic_bytes(path, encoded)


def _ordered(records):
    groups = defaultdict(list)
    for record in records:
        groups[record['source_url']].append(record)
    for group in groups.values():
        group.sort(key=lambda r: (not 8 <= r['excerpt_start_seconds_label'] <= 65,
                                  r['excerpt_start_seconds_label']))
    width = max(map(len, groups.values())) if groups else 0
    return [group[i] for i in range(width) for group in groups.values() if i < len(group)]


@register('citizen_dj')
class CitizenDJConnector:
    name = 'citizen_dj'
    enforces_deadline = True

    def __init__(self, config=None):
        self.config = config or {}
        self.cache = Path(self.config.get('cache_root', common.LIBRARY / 'sources/citizen_dj'))
        self.skipped = 0
        self.truncated = False
        self.deadline = None

    def _finite_timeout(self, value, name):
        value = float(value)
        if not np.isfinite(value) or value <= 0:
            raise ValueError(f'{name} must be a finite positive number')
        return value

    def _set_timeout(self):
        value = self.config.get('timeout_s')
        timeout = self._finite_timeout(300 if value is None else value, 'timeout_s')
        configured = self.config.get('deadline_s')
        if configured is None:
            self.deadline = time.monotonic() + timeout
        else:
            self.deadline = time.monotonic() + self._finite_timeout(configured, 'deadline_s')

    def _remaining(self):
        if self.deadline is None:
            return None
        remaining = float(self.deadline) - time.monotonic()
        if not np.isfinite(remaining):
            raise ValueError('deadline must be finite')
        if remaining <= 0:
            raise TimeoutError('Crawl deadline reached')
        return remaining

    def _validate_url(self, url):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.query or parsed.fragment:
            raise ValueError('Only fixed HTTPS URLs without query or fragment are allowed')
        if parsed.username is not None or parsed.password is not None or parsed.port is not None:
            raise ValueError('URL credentials and explicit ports are not allowed')
        # Leading and trailing slashes are normal; reject only current/parent
        # traversal in internal path segments.
        segments = parsed.path.split('/')[1:-1] if parsed.path.startswith('/') else parsed.path.split('/')
        if any(segment in ('.', '..') for segment in segments) or '%' in parsed.path:
            raise ValueError('Invalid URL path')
        host = parsed.hostname
        if host == 'citizen-dj.labs.loc.gov':
            allowed = {f'/{name}/use/' for name in COLLECTIONS.values()}
            if parsed.path not in allowed:
                raise ValueError('URL is not an allowed Citizen DJ collection page')
        elif host == 's3.amazonaws.com':
            prefixes = tuple(ASSETS[len('https://s3.amazonaws.com'):] + name + '/'
                             for name in COLLECTIONS.values())
            if not (parsed.path.startswith(prefixes) and parsed.path.endswith('.wav')):
                raise ValueError('URL is not a published Citizen DJ audio asset')
        else:
            raise ValueError('URL host is not allowed')
        return url

    def _sleep_budget(self, delay):
        remaining = self._remaining()
        if remaining is not None and delay > remaining:
            raise TimeoutError('Crawl deadline reached')
        time.sleep(delay)

    def _get(self, url, max_bytes):
        max_bytes = int(max_bytes)
        if max_bytes <= 0:
            raise ValueError('Invalid download size limit')
        url = self._validate_url(url)
        headers = {'User-Agent': 'BeatLab/0.1 (Citizen-DJ music research)'}
        attempts = 0
        while True:
            attempts += 1
            remaining = self._remaining()
            timeout = 10.0 if remaining is None else min(10.0, remaining)
            try:
                with requests.get(url, headers=headers, stream=True, timeout=(timeout, timeout),
                                  allow_redirects=False) as response:
                    if 500 <= response.status_code <= 599 or response.status_code == 429:
                        if attempts < 3:
                            raise _RetryableHTTP(response.status_code)
                    response.raise_for_status()
                    if response.status_code != 200:
                        raise ValueError(f'Unexpected HTTP {response.status_code}')
                    try:
                        advertised = int(response.headers.get('Content-Length', '0'))
                        if advertised > max_bytes:
                            raise ValueError('Source exceeds download size limit')
                    except (TypeError, ValueError):
                        if response.headers.get('Content-Length') is not None:
                            raise
                    chunks = []
                    size = 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError('Source exceeds download size limit')
                        self._remaining()
                        chunks.append(chunk)
                    return b''.join(chunks)
            except _RetryableHTTP:
                if attempts >= 3:
                    raise
            except (requests.ConnectionError, requests.Timeout):
                if attempts >= 3:
                    raise
            self._sleep_budget((0.5, 1.0)[attempts - 1])

    def discover(self, collection):
        if self.deadline is None:
            self._set_timeout()
        if collection not in COLLECTIONS:
            raise ValueError('Supported collections: blues, jazz')
        page_url = f'{SITE}/{COLLECTIONS[collection]}/use/'
        self._validate_url(page_url)
        page = self._get(page_url, 2_000_000)
        html = page.decode('utf-8')
        records = parse_catalog(html, collection)
        if not records:
            raise ValueError('No published WAV excerpts found; check catalog schema')
        sha = _sha_bytes(page)
        folder = self.cache / collection
        folder.mkdir(parents=True, exist_ok=True)
        snapshot = folder / f'catalog-{sha[:16]}.html'
        if not snapshot.exists() or _sha_file(snapshot) != sha:
            _atomic_bytes(snapshot, page)
        return {'collection': collection, 'collection_url': page_url, 'catalog_sha256': sha,
                'catalog_snapshot': str(snapshot.resolve()), 'records': _ordered(records)}

    def _read_catalog(self, catalog, collection):
        if collection not in COLLECTIONS:
            raise ValueError('Supported collections: blues, jazz')
        if not isinstance(catalog, dict) or catalog.get('collection') != collection:
            raise ValueError('Catalog does not match requested collection')
        snapshot = Path(catalog.get('catalog_snapshot', ''))
        page = snapshot.read_bytes()
        sha = _sha_bytes(page)
        if sha != catalog.get('catalog_sha256'):
            raise ValueError('Catalog snapshot checksum mismatch')
        expected_url = f'{SITE}/{COLLECTIONS[collection]}/use/'
        if catalog.get('collection_url') != expected_url:
            raise ValueError('Catalog metadata mismatch')
        records = _ordered(parse_catalog(page.decode('utf-8'), collection))
        if catalog.get('records') != records:
            raise ValueError('Catalog records do not match snapshot')
        return page, records, snapshot, sha

    def _target(self, collection, record):
        key = hashlib.sha256(record['media_url'].encode()).hexdigest()[:20]
        target = self.cache / collection / key
        return target, target / 'download.wav', target / 'source.json'

    def _valid_audio(self, path):
        info = sf.info(path)
        if info.frames <= 0 or info.channels not in (1, 2) or info.samplerate <= 0:
            raise ValueError('Invalid audio excerpt')
        seen = 0
        with sf.SoundFile(path) as handle:
            while seen < info.frames:
                block = handle.read(4096, dtype='float64', always_2d=True)
                if block.size == 0:
                    break
                if not np.isfinite(block).all():
                    raise ValueError('Audio contains non-finite samples')
                seen += block.shape[0]
                self._remaining()
        if seen != info.frames:
            raise ValueError('Truncated audio decode')
        return info

    def _download(self, collection, record):
        target, audio, _ = self._target(collection, record)
        self._validate_url(record['media_url'])
        prefix = ASSETS + COLLECTIONS[collection] + '/'
        if not record['media_url'].startswith(prefix) or not record['media_url'].endswith('.wav'):
            raise ValueError('Media URL is not in published collection')
        target.mkdir(parents=True, exist_ok=True)
        part = target / 'download.part'
        try:
            pause = float(self.config.get('pause_s', 1))
            if not np.isfinite(pause) or pause < 0:
                raise ValueError('pause_s must be finite and nonnegative')
            self._sleep_budget(pause)
            blob = self._get(record['media_url'], 32_000_000)
            part.write_bytes(blob)
            info = self._valid_audio(part)
            part.replace(audio)
            return audio, info
        finally:
            with contextlib.suppress(FileNotFoundError):
                part.unlink()

    @staticmethod
    def _identity_fields(data):
        return (data.get('media_url'), data.get('source_url'), data.get('collection'),
                data.get('collection_url'))

    def _usable_cache(self, data, audio, record, collection, need_sha=None):
        if not isinstance(data, dict):
            return False
        try:
            if not audio.is_file():
                return False
            if self._identity_fields(data) != (
                    record['media_url'], record['source_url'], collection,
                    f'{SITE}/{COLLECTIONS[collection]}/use/'):
                return False
            if not data.get('download_sha256') or not data.get('md5') or not data.get('retrieved_at'):
                return False
            if need_sha is not None and data.get('catalog_sha256') != need_sha:
                return False
            return _sha_file(audio) == data['download_sha256'] and \
                common.md5_file(audio) == data['md5']
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def cached_entry(self, record):
        if not isinstance(record, dict) or 'media_url' not in record:
            return None
        key = hashlib.sha256(record['media_url'].encode()).hexdigest()[:20]
        for meta in self.cache.glob(f'*/{key}/source.json'):
            audio = meta.with_name('download.wav')
            try:
                data = json.loads(meta.read_text(encoding='utf-8'))
                collection = data.get('collection')
                if collection in COLLECTIONS and self._usable_cache(data, audio, record, collection):
                    return data
            except (OSError, ValueError):
                continue
        return None

    def fetch_entry(self, collection, record, catalog):
        _, records, snapshot, page_sha = self._read_catalog(catalog, collection)
        if record not in records:
            raise ValueError('Record is not present in validated catalog')
        target, audio, meta = self._target(collection, record)
        page_url = catalog['collection_url']
        cached = None
        if audio.is_file() and meta.is_file():
            try:
                candidate = json.loads(meta.read_text(encoding='utf-8'))
                # A changed catalog snapshot alone does not invalidate unchanged
                # audio; rights and membership have just been reverified.
                if self._usable_cache(candidate, audio, record, collection):
                    cached = candidate
            except (OSError, ValueError):
                cached = None
        if cached is None:
            audio, info = self._download(collection, record)
            download_sha = _sha_file(audio)
            md5 = common.md5_file(audio)
            provenance = {**record, 'collection': collection, 'collection_url': page_url,
                          'credit': CREDIT, 'catalog_sha256': page_sha,
                          'catalog_snapshot': str(snapshot), 'retrieved_at': common.now_iso(),
                          'download_sha256': download_sha, 'md5': md5,
                          'offset_precision': 'page timestamp label; remix offset preserved separately, not sample-accurate alignment'}
            _atomic_json(meta, provenance)
        else:
            info = self._valid_audio(audio)
            download_sha = cached['download_sha256']
            md5 = cached['md5']
            provenance = {**record, 'collection': collection, 'collection_url': page_url,
                          'credit': CREDIT, 'catalog_sha256': page_sha,
                          'catalog_snapshot': str(snapshot), 'retrieved_at': cached['retrieved_at'],
                          'download_sha256': download_sha, 'md5': md5,
                          'offset_precision': cached['offset_precision']}
            try:
                old = json.loads(meta.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                old = None
            # Preserve retrieval time and avoid churning source.json when provenance is unchanged.
            if old != provenance:
                _atomic_json(meta, provenance)
        return dict(orig_path=str(audio.resolve()), md5=md5, size_bytes=audio.stat().st_size,
                    duration_s=info.duration, title=record['title'], source_url=record['source_url'],
                    category='loops', genre=collection,
                    license='Public domain — LOC Citizen DJ collection', provenance=provenance,
                    rights=dict(state='allowed', basis='loc_citizen_dj_collection_statement'))

    def scan(self, path, known_md5s=None):
        if path not in COLLECTIONS:
            raise ValueError('Use --path blues or --path jazz')
        limit = self.config.get('limit')
        limit = 10 if limit is None else int(limit)
        if not 0 <= limit <= 100:
            raise ValueError('Citizen DJ limit must be between 0 and 100')
        self.skipped = 0
        self.truncated = False
        if limit == 0:
            return []
        old_deadline = self.deadline
        self._set_timeout()
        try:
            catalog = self.discover(path)
            known = set(known_md5s or ())
            entries = []
            processed = 0
            for record in catalog['records']:
                if processed >= limit:
                    self.truncated = True
                    break
                if time.monotonic() >= self.deadline:
                    self.truncated = True
                    break
                target, audio, meta = self._target(path, record)
                cached = {}
                if audio.exists() and meta.exists():
                    try:
                        cached = json.loads(meta.read_text(encoding='utf-8'))
                    except (ValueError, OSError):
                        pass
                valid = self._usable_cache(cached, audio, record, path)
                if valid and cached.get('md5') in known:
                    self.skipped += 1
                    continue
                processed += 1
                try:
                    print(f"[citizen_dj] {path}: {record['title']} @ "
                          f"{record['excerpt_start_seconds_label']}s", flush=True)
                    entry = self.fetch_entry(path, record, catalog)
                    if entry['md5'] in known:
                        self.skipped += 1
                        continue
                    known.add(entry['md5'])
                    entries.append(entry)
                except Exception as exc:
                    entries.append(dict(orig_path=str(audio), source_url=record['source_url'],
                                        error=str(exc)))
            return entries
        finally:
            self.deadline = old_deadline


class _RetryableHTTP(Exception):
    def __init__(self, status):
        super().__init__(f'HTTP {status}')
        self.status = status
