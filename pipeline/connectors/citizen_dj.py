"""Discover historical musical excerpts from Citizen DJ's public download pages."""
from collections import defaultdict
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlparse

import requests
import soundfile as sf

import common
from connectors import register

SITE='https://citizen-dj.labs.loc.gov'
COLLECTIONS={'blues':'loc-jukebox-blues','jazz':'loc-jukebox-jazz'}
ASSETS='https://s3.amazonaws.com/citizen-dj-assets.labs.loc.gov/audio/samplepacks/'
CREDIT='Citizen DJ Project, Library of Congress, National Jukebox.'


class _CatalogParser(HTMLParser):
    def __init__(self,prefix):
        super().__init__()
        self.prefix=prefix;self.records=[];self.seen=set()
        self.in_title=False;self.title='';self.item='';self.start=None;self.last=None

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs);href=attrs.get('href','')
        if tag=='h4':
            self.in_title=True;self.title='';self.item='';self.start=None;self.last=None
        if tag!='a':return
        parsed=urlparse(href)
        if parsed.hostname=='www.loc.gov' and parsed.path.startswith('/item/'):
            self.item='https://www.loc.gov'+parsed.path
        if 'download' in attrs and href.startswith(self.prefix) and href.endswith('.wav'):
            if '..' in parsed.path.split('/') or href in self.seen:return
            if not self.item or self.start is None:return
            self.last=dict(title=self.title.strip(),source_url=self.item,media_url=href,
                           excerpt_start_seconds_label=self.start)
            self.records.append(self.last);self.seen.add(href)
        if self.last is not None and parsed.path.endswith('/remix/'):
            value=parse_qs(parsed.query).get('itemStart',[''])[0]
            if value.isdigit():self.last['remix_item_start_ms']=int(value)

    def handle_endtag(self,tag):
        if tag=='h4':self.in_title=False

    def handle_data(self,data):
        if self.in_title:self.title+=data
        match=re.search(r'Excerpt starting at (\d+):(\d{2})(?::(\d{2}))?',data)
        if match:
            a,b,c=match.groups()
            self.start=int(a)*60+int(b) if c is None else int(a)*3600+int(b)*60+int(c)


def parse_catalog(html,collection):
    if collection not in COLLECTIONS:raise ValueError('Supported collections: blues, jazz')
    if ('identified to be in the public domain' not in html or
            'free to use and reuse without restriction' not in html):
        raise ValueError('Collection rights statement is absent or has changed')
    parser=_CatalogParser(ASSETS+COLLECTIONS[collection]+'/')
    parser.feed(html)
    return parser.records


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path,data):
    temp=path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    temp.replace(path)


@register('citizen_dj')
class CitizenDJConnector:
    name='citizen_dj'
    enforces_deadline=True

    def __init__(self,config=None):
        self.config=config or {}
        self.cache=Path(self.config.get('cache_root',common.LIBRARY/'sources/citizen_dj'))
        self.skipped=0;self.truncated=False;self.deadline=None

    def _get(self,url,max_bytes):
        # URLs originate only from the fixed collection page and its published WAV links.
        with requests.get(url,headers={'User-Agent':'BeatLab/0.1 (Citizen-DJ music research)'},
                          stream=True,timeout=(10,30),allow_redirects=False) as response:
            response.raise_for_status()
            if response.status_code!=200:raise ValueError(f'Unexpected HTTP {response.status_code}')
            chunks=[];size=0
            for chunk in response.iter_content(65536):
                size+=len(chunk)
                if size>max_bytes:raise ValueError('Source exceeds download size limit')
                if self.deadline and time.monotonic()>self.deadline:raise TimeoutError('Crawl deadline reached')
                chunks.append(chunk)
            return b''.join(chunks)

    def scan(self,path,known_md5s=None):
        if path not in COLLECTIONS:raise ValueError('Use --path blues or --path jazz')
        limit=self.config.get('limit')
        limit=10 if limit is None else int(limit)
        if not 0<=limit<=100:raise ValueError('Citizen DJ limit must be between 0 and 100')
        self.skipped=0;self.truncated=False
        if limit==0:return []
        timeout=self.config.get('timeout_s') or 300
        self.deadline=time.monotonic()+timeout
        page_url=f'{SITE}/{COLLECTIONS[path]}/use/'
        page=self._get(page_url,2_000_000)
        records=parse_catalog(page.decode('utf-8'),path)
        if not records:raise ValueError('No published WAV excerpts found; check catalog schema')
        folder=self.cache/path;folder.mkdir(parents=True,exist_ok=True)
        page_sha=hashlib.sha256(page).hexdigest()
        snapshot=folder/f'catalog-{page_sha[:16]}.html'
        if not snapshot.exists():snapshot.write_bytes(page)
        # Take one phrase per recording before taking another from the same recording.
        groups=defaultdict(list)
        for record in records:groups[record['source_url']].append(record)
        for group in groups.values():
            group.sort(key=lambda r:(not 8<=r['excerpt_start_seconds_label']<=65,r['excerpt_start_seconds_label']))
        ordered=[group[i] for i in range(max(map(len,groups.values()))) for group in groups.values() if i<len(group)]
        known=set(known_md5s or ());entries=[];attempts=0
        for record in ordered:
            if attempts>=limit or time.monotonic()>self.deadline:
                self.truncated=True;break
            url=record['media_url'];key=hashlib.sha256(url.encode()).hexdigest()[:20]
            target=folder/key;audio=target/'download.wav';meta=target/'source.json'
            cached={}
            if audio.exists() and meta.exists():
                try:cached=json.loads(meta.read_text())
                except (ValueError,OSError):pass
            valid=bool(cached.get('download_sha256')) and audio.is_file() and _sha(audio)==cached['download_sha256']
            if valid and cached.get('md5') in known:
                self.skipped+=1;continue
            attempts+=1
            try:
                if not valid:
                    print(f"[citizen_dj] {path}: {record['title']} @ {record['excerpt_start_seconds_label']}s",flush=True)
                    time.sleep(max(0,float(self.config.get('pause_s',1))))
                    blob=self._get(url,32_000_000)
                    target.mkdir(exist_ok=True)
                    temp=target/'download.part'
                    try:
                        temp.write_bytes(blob)
                        info=sf.info(temp)
                        if not info.frames or info.channels not in (1,2):raise ValueError('Invalid audio excerpt')
                        temp.replace(audio)
                    finally:
                        temp.unlink(missing_ok=True)
                info=sf.info(audio);md5=common.md5_file(audio)
                provenance={**record,'collection':path,'collection_url':page_url,'credit':CREDIT,
                            'catalog_sha256':page_sha,'catalog_snapshot':str(snapshot.resolve()),
                            'retrieved_at':common.now_iso(),'download_sha256':_sha(audio),'md5':md5,
                            'offset_precision':'page timestamp label; remix offset preserved separately, not sample-accurate alignment'}
                _json(meta,provenance)
                if md5 in known:
                    self.skipped+=1;continue
                known.add(md5)
                entries.append(dict(orig_path=str(audio.resolve()),md5=md5,size_bytes=audio.stat().st_size,
                                    duration_s=info.duration,title=record['title'],source_url=record['source_url'],
                                    category='loops',genre=path,license='Public domain — LOC Citizen DJ collection',
                                    provenance=provenance,rights=dict(state='allowed',basis='loc_citizen_dj_collection_statement')))
            except Exception as exc:
                entries.append(dict(orig_path=str(audio),source_url=record['source_url'],error=str(exc)))
        return entries
