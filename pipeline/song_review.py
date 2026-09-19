"""A small listening and explicit layer-feedback view for rendered scores."""
import html
import json
from pathlib import Path
from urllib.parse import quote


def build_page(song: Path) -> str:
    manifest = json.loads((song/'run_manifest.json').read_text())
    title = html.escape(manifest['title'])
    base = '/media/' + quote(song.name) + '/'
    rows = []
    for track in manifest['tracks']:
        tid = html.escape(track['id'], quote=True)
        name = html.escape(track.get('name', track['id']))
        rows.append(f'<label>{name}<select data-track="{tid}"><option value="0">保持</option>'
                    '<option value="-3">轻一点 −3 dB</option><option value="-6">明显减弱 −6 dB</option>'
                    '<option value="-60">接近静音 −60 dB</option></select></label>')
    extras = ''.join(f'<p>{label}<audio controls preload="none" src="{base}{name}"></audio></p>'
                     for name, label in [('original-phrase.wav','原创原句'),
                                         ('sample-flip-solo.wav','翻采层独奏')]
                     if (song/name).is_file())
    config = json.dumps(song.name).replace('<', '\\u003c')
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · BeatLab</title>
<style>body{{background:#181c21;color:#e8e5de;font:17px/1.6 system-ui;max-width:840px;margin:48px auto;padding:24px}}
a{{color:#eac578}}h1{{font-size:36px}}audio{{display:block;width:100%;margin:12px 0 28px}}
label{{display:flex;justify-content:space-between;gap:20px;border-bottom:1px solid #43464a;padding:10px 0}}
select,button{{font:inherit;padding:8px;background:#ece6d8;border:0;border-radius:5px}}
button{{margin-top:24px;cursor:pointer}}small{{color:#b8b6ae}}#status{{white-space:pre-wrap}}</style>
<a href="/">← 所有作品</a><h1>{title}</h1>
<small>{manifest['bpm']} BPM · {manifest['bars']} 小节 · {manifest['duration_seconds']:.1f} 秒 · 待试听</small>
<audio controls preload="metadata" src="{base}full_mix.wav"></audio>{extras}
<h2>哪一层太抢？</h2><p>选要减弱的层，生成一个可比较的新版本。原版保留，其他层音量保持。</p>
{''.join(rows)}<button id="make">生成反馈版</button><p id="status" role="status"></p><div id="result"></div>
<small>这里只调整指定层音量；不会自动改词、选新采样或重新编曲。Ableton 实际发声与听感另行验收。</small>
<script>const runId={config};const btn=document.getElementById('make');
btn.onclick=async()=>{{const gains={{}};document.querySelectorAll('[data-track]').forEach(x=>{{if(+x.value)gains[x.dataset.track]=+x.value;}});
const status=document.getElementById('status');if(!Object.keys(gains).length){{status.textContent='先选一层要怎么改。';return;}}
btn.disabled=true;status.textContent='正在渲染新版本…';
try{{const r=await fetch('/api/revise',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{run_id:runId,gains_db:gains}})}});
const data=await r.json();if(!r.ok)throw new Error(data.error||'渲染失败');
status.textContent=data.reused?'已有同样的反馈版，直接试听。':'新版本已生成，可与上面的原版比较。';
const result=document.getElementById('result');result.replaceChildren();const link=document.createElement('a');link.href=data.review_url;link.textContent='打开反馈版';
const audio=document.createElement('audio');audio.controls=true;audio.src='/media/'+encodeURIComponent(data.review_url.split('/').pop())+'/full_mix.wav';result.append(link,audio);
}}catch(e){{status.textContent='未完成：'+e.message;}}finally{{btn.disabled=false;}}}};</script></html>'''
