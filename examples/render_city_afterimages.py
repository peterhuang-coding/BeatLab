"""Render/recover the fixed, locally curated ten-draft audition collection.

Uses reviewed composition code and existing audio/export APIs. Does not publish,
download, overwrite a finished song, or manufacture user feedback.
"""
from pathlib import Path
from collections import Counter
from copy import deepcopy
import argparse
import json
import math
import shutil
import subprocess
import numpy as np
import soundfile as sf
from pipeline.song import render_score
from pipeline.ableton_export import validate_song, export_song
from examples.real_record_delivery import (sha256, collect_samples, write_phrase_midi,
    build_rack, build_listening, copy_sources)

COLLECTION='city-afterimages-20260927'
CHINESE_DIRECTIONS=['明亮 soul 翻采与切分鼓','爵士短句与轻巧错拍','Blues 问答、松弛律动','铜管切片与强烈段落切换','半拍现代鼓、空间感','亲密碎句、稀疏到丰满','明亮 broken funk','Ragtime 玩味切分与停顿','Two-step 与倒放引子','长句与切片重现、温暖收尾']


def plan_for(score, manifest):
    """Check actual used cuts/events without imposing an old song's role counts."""
    tracks={t['id']:t for t in score['tracks']}
    rendered={t['id']:t for t in manifest['tracks']}
    placements=[]; slices=[]
    flip=score['sample_flip']
    for raw in flip['slices']:
        sid=raw['id']
        if sid not in tracks: continue
        sl={**raw,'track':sid}
        source=Path(sl['source_file']);cut=Path(sl['file'])
        assert sha256(source)==sl['source_sha256']
        assert sha256(cut)==sl['sha256']==rendered[sid]['source_sha256']
        assert (Path(score['sample_root'])/tracks[sid]['sample']).resolve()==cut.resolve()
        info=sf.info(source)
        assert 0<=round(sl['start_seconds']*info.samplerate)<round(sl['end_seconds']*info.samplerate)<=info.frames
        info=sf.info(cut)
        assert info.frames==round(sl['target_beats']*60/score['bpm']*info.samplerate)
        slices.append(sl)
    ids={s['id'] for s in slices}
    for p in flip['placements']:
        sid=p.get('slice_id',p.get('slice'))
        assert sid in ids and p.get('note',60)==60
        assert p['beat']>=0 and 0<p['duration_beats'] and p['beat']+p['duration_beats']<=4*score['bars']
        assert p['duration_beats']<=next(s['target_beats'] for s in slices if s['id']==sid)
        placements.append({**p,'slice':sid})
    key=lambda sid,e:(sid,round(e['beat'],6),round(e['duration_beats'],6),round(e.get('velocity',.7),6),e.get('note',60))
    assert Counter(key(p['slice'],p) for p in placements)==Counter(key(sid,e) for sid in ids for e in tracks[sid]['events'])
    ordered=sorted(placements,key=lambda p:p['beat'])
    assert all(a['beat']+a['duration_beats']<=b['beat']+1e-7 for a,b in zip(ordered,ordered[1:]))
    return dict(slices=slices,placements=placements,source_records=deepcopy(flip['source_records']),
                bars=score['bars'],bpm=score['bpm'],sample_rate=manifest['sample_rate'])


def loudness(path):
    result=subprocess.run(['ffmpeg','-hide_banner','-nostdin','-i',str(path),'-af',
        'loudnorm=I=-20:TP=-1:LRA=11:print_format=json','-f','null','-'],capture_output=True,text=True,check=True,timeout=120)
    raw=result.stderr[result.stderr.rfind('{'):];d,_=json.JSONDecoder().raw_decode(raw)
    return dict(lufs=float(d['input_i']),true_peak_db=float(d['input_tp']))


def run(root):
    from examples.ten_beat_scores import build_score, RECIPES
    root=Path(root).resolve(); index=root/'library/instruments'/COLLECTION/'prepared-index.json'
    collection=root/'exports'/COLLECTION; collection.mkdir(parents=True,exist_ok=True)
    audio_dir=collection/'audio';audio_dir.mkdir(exist_ok=True)
    results=[]
    for prepared_path in json.loads(index.read_text()):
        prepared=json.loads(Path(prepared_path).read_text())
        if prepared['number'] not in RECIPES: continue
        score=build_score(prepared)
        score['instrument_sources']=deepcopy(prepared['instrument_sources'])
        # Optional instrument tracks without notes are not delivered as silent stems.
        score['tracks']=[t for t in score['tracks'] if t['events']]
        song=root/'beats'/prepared['run_id'];out=collection/'Projects'/prepared['run_id']
        if not (song/'run_manifest.json').exists():
            render_score(score,song)
        else:
            if json.loads((song/'score.json').read_text())!=score:
                raise ValueError('Existing song uses a different recipe; choose a new version')
        manifest=json.loads((song/'run_manifest.json').read_text());qa=validate_song(song);plan=plan_for(score,manifest)
        if not (out/'delivery_manifest.json').exists():
            if out.exists():raise FileExistsError('Incomplete package preserved for inspection: '+str(out))
            out.mkdir(parents=True)
            export=export_song(song,out/'AbletonProject',set_name=prepared['title'])
            pads=collect_samples(plan,out/'ChopRack')
            write_phrase_midi(plan,out/'ChopRack/phrase-chops.mid')
            build_rack(pads,out/'ChopRack',out/'ChopRack',preset_name=prepared['title']+' Chops')
            listening=build_listening(song,plan,out/'Listening')
            sources=copy_sources(plan,out/'Sources')
            (out/'source_map.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n')
            (out/'prepared.json').write_text(json.dumps(prepared,ensure_ascii=False,indent=2)+'\n')
            (out/'README.md').write_text(f'# {prepared["title"]}\n\n本地试听草稿，尚未用户Keep或发行验收。\n\nAbletonProject内ALS是与原混音一致的可编辑音频分轨；Source内含score与MIDI。ChopRack含真实切片、原生鼓垫预设及整首切片触发MIDI，可继续改切法。钢琴/Bass/鼓的MIDI没有自动在ALS内重建音源。Live打开、发声、保存、重开与回渲染仍未验。\n\nSources/source_map提供录音来源；Listening可对照原片段→处理切片→成品。不是纯净人声分离，不声称已识别不同歌词。原厂乐器不作为独立素材包对外销售。\n')
            files=[dict(path=str(p.relative_to(out)),sha256=sha256(p),bytes=p.stat().st_size) for p in sorted(out.rglob('*')) if p.is_file()]
            (out/'delivery_manifest.json').write_text(json.dumps(dict(song=str(song),qa=qa,files=files,
                sources=sources,export=export,keep=None,publication='not published'),ensure_ascii=False,indent=2)+'\n')
        levels=loudness(song/'full_mix.wav')
        y,sr=sf.read(song/'full_mix.wav',dtype='float32',always_2d=True)
        assert sr==44100 and y.shape[1]==2 and np.isfinite(y).all() and np.max(np.abs(y))<1
        assert 45<=len(y)/sr<=62
        results.append(dict(prepared=prepared,song=str(song),project=str(out),qa=qa,levels=levels,
                            mix_sha256=sha256(song/'full_mix.wav'),score_sha256=sha256(song/'score.json')))
        print('render/export',prepared['number'],prepared['title'],levels,flush=True)
    # One linear gain per preview; preserve source masters/stems. Never limiter-match.
    target=min(-20.0,min(r['levels']['lufs'] for r in results))
    tracks=[]
    for r in results:
        d=r['prepared'];gain_db=target-r['levels']['lufs'];source=Path(r['song'])/'full_mix.wav'
        y,sr=sf.read(source,dtype='float32',always_2d=True);wav=audio_dir/(d['run_id']+'.wav');mp3=wav.with_suffix('.mp3')
        if not wav.exists():sf.write(wav,y*10**(gain_db/20),sr,subtype='PCM_24')
        if not mp3.exists():subprocess.run(['ffmpeg','-v','error','-nostdin','-i',str(wav),'-codec:a','libmp3lame','-b:a','256k',str(mp3)],check=True,timeout=90)
        actual=loudness(wav);assert abs(actual['lufs']-target)<.15
        score=json.loads((Path(r['song'])/'score.json').read_text());sections=score.get('sections',[])
        change=score['metadata']['middle_switch']['beat']*60/d['bpm']
        tracks.append(dict(id=d['run_id'],number=int(d['number']),title=d['title'],bpm=d['bpm'],duration_seconds=len(y)/sr,
            direction=CHINESE_DIRECTIONS[int(d['number'])-1],audio=str(mp3.relative_to(collection)),original_audio=str(wav.relative_to(collection)),
            source_summary=[dict(title=s['title'],source_url=s['source_url']) for s in d['source_records']],
            editable_path=r['project'],version='v1',mix_sha256=r['mix_sha256'],change_point_seconds=change,
            listening_gain_db=gain_db,listening_lufs=actual['lufs'],matched_audio_sha256=sha256(wav),keep=None))
    data=dict(id=COLLECTION,title='城市余像 · City Afterimages',subtitle=f'主题试听：已完成{len(tracks)}/10首，其余仍待编排修正。先听整体，再按你的反馈继续制作。',tracks=tracks,
              purpose='human style audition before targeted revisions',loudness_target_lufs=target,keep=None)
    (collection/'collection.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    (collection/'render-evidence.json').write_text(json.dumps([{k:v for k,v in r.items() if k!='prepared'} for r in results],ensure_ascii=False,indent=2)+'\n')
    return collection


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--media-root',required=True,type=Path)
    print(run(p.parse_args().media_root))
