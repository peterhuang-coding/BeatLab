"""A small listening and explicit layer-feedback view for rendered scores.

页面包含两个相互独立的能力:
- 既有「分层音量反馈版」：选层 → POST /api/revise（行为保持不变）；
- 新增「试听笔记」：抓取主音频当前播放时间 → 选类别/写文字 →
  POST /api/listening-notes，按页面加载时读取到的 full_mix.wav SHA256 绑定版本，
  历史笔记点击即跳转。笔记只做人工记录，不触发任何自动改动。
"""
import html
import json
from pathlib import Path
from urllib.parse import quote

from listening_notes import CATEGORIES as NOTE_CATEGORIES
from song_provenance import render_source_section

# code 顺序需与 listening_notes.CATEGORIES 一致（服务端校验白名单）
CATEGORY_LABELS = (
    ("noisy", "太嘈杂"),
    ("repetitive", "重复"),
    ("drums", "鼓组"),
    ("transition", "过渡"),
    ("like", "喜欢"),
    ("other", "其他"),
)
assert [code for code, _ in CATEGORY_LABELS] == list(NOTE_CATEGORIES)

SONG_CSS = """body{background:#181c21;color:#e8e5de;font:17px/1.6 system-ui;max-width:840px;margin:48px auto;padding:24px}
a{color:#eac578}h1{font-size:36px}audio{display:block;width:100%;margin:12px 0 28px}
label{display:flex;justify-content:space-between;gap:20px;border-bottom:1px solid #43464a;padding:10px 0}
select,button{font:inherit;padding:8px;background:#ece6d8;color:#181c21;border:0;border-radius:5px}
button{margin-top:24px;cursor:pointer}button:disabled{opacity:.6;cursor:default}
small{color:#b8b6ae}#status{white-space:pre-wrap}
.notes{margin-top:44px;border-top:1px solid #43464a;padding-top:24px}
.fld{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:12px 0}
.notes .fld label{border:0;padding:0;justify-content:flex-start;min-width:104px}
.fld.col{flex-direction:column;align-items:stretch}
input,textarea{font:inherit;padding:8px;background:#ece6d8;color:#181c21;border:0;border-radius:5px}
#note-time{width:110px}textarea{resize:vertical}
#capture-time{margin-top:0}#save-note{margin-top:8px;background:#eac578}
.ctx{color:#b8b6ae;font-size:14px;word-break:break-all}
#notes-status{min-height:22px;font-size:14px}
#notes-status.err{color:#e08a8a}#notes-status.ok{color:#9adbb8}
#notes-history{list-style:none;padding:0;margin:12px 0}
#notes-history li{display:grid;grid-template-columns:88px 88px minmax(0,1fr);gap:10px;align-items:baseline;border-bottom:1px solid #43464a;padding:10px 0}
#notes-history li.empty{display:block;color:#b8b6ae}
.seek{margin-top:0;padding:4px 10px;font-variant-numeric:tabular-nums}
.cat{color:#eac578}.note-text{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.when{color:#b8b6ae;grid-column:1/-1}"""

PAGE_JS = r"""function $(id){return document.getElementById(id);}
function fmtTime(t){return (Number(t)||0).toFixed(1)+' 秒';}
function newRequestId(){
  if(window.crypto && typeof crypto.randomUUID==='function'){
    try{return crypto.randomUUID();}catch(e){}
  }
  return 'rn-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10)+'-'+Math.random().toString(36).slice(2,6);
}
const CAT_LABELS={};
CFG.categoryLabels.forEach(pair=>{CAT_LABELS[pair[0]]=pair[1];});

const mainAudio=$('main-audio');
const noteTime=$('note-time'), noteCategory=$('note-category'), noteText=$('note-text');
const notesContext=$('notes-context'), notesStatus=$('notes-status'), notesHistory=$('notes-history');
const saveBtn=$('save-note');
// 版本上下文只在页面加载时读取一次；保存始终绑定这个 SHA，不静默刷新
const version={sha:null,duration:null};
let requestId=newRequestId();
let draftSignature=null;

function setNotesStatus(msg,isError){
  notesStatus.textContent=msg;
  notesStatus.className=isError?'err':'ok';
}
function draftSignatureOf(){
  return JSON.stringify([noteTime.value, noteCategory.value, noteText.value.trim()]);
}
function renderNote(note){
  const li=document.createElement('li');
  const seek=document.createElement('button');
  seek.type='button'; seek.className='seek';
  seek.textContent=fmtTime(note.time_seconds);
  seek.setAttribute('aria-label','跳转到 '+fmtTime(note.time_seconds));
  seek.addEventListener('click',()=>{
    const t=Number(note.time_seconds);
    if(Number.isFinite(t)){mainAudio.currentTime=Math.max(0,t);}
  });
  const cat=document.createElement('span');
  cat.className='cat'; cat.textContent=CAT_LABELS[note.category]||note.category;
  const text=document.createElement('p');
  text.className='note-text'; text.textContent=note.text;
  const when=document.createElement('small');
  when.className='when';
  when.textContent=note.created_at?('UTC '+note.created_at):'';
  li.append(seek,cat,text,when);
  return li;
}
function renderHistory(notes){
  notesHistory.replaceChildren();
  if(!notes.length){
    const empty=document.createElement('li');
    empty.className='empty'; empty.textContent='此版本暂无笔记。';
    notesHistory.appendChild(empty);
    return;
  }
  notes.forEach(note=>notesHistory.appendChild(renderNote(note)));
}
async function loadVersion(){
  try{
    const r=await fetch('/api/listening-notes?run_id='+encodeURIComponent(CFG.runId),{cache:'no-store'});
    const data=await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(data.error||('HTTP '+r.status));
    version.sha=data.mix_sha256;
    version.duration=data.duration_seconds;
    noteTime.max=String(data.duration_seconds);
    notesContext.textContent='试听版本 '+data.mix_sha256.slice(0,12)+' · '+fmtTime(data.duration_seconds);
    notesContext.title='音频 SHA256：'+data.mix_sha256;
    renderHistory(data.notes||[]);
    setNotesStatus('',false);
  }catch(e){
    version.sha=null;
    notesContext.textContent='无法读取当前音频版本。';
    setNotesStatus('读取版本失败：'+e.message+'。保存笔记需先启动本地反馈服务。',true);
  }
}
$('capture-time').addEventListener('click',()=>{
  const t=(mainAudio.currentTime&&Number.isFinite(mainAudio.currentTime))?mainAudio.currentTime:0;
  noteTime.value=t.toFixed(2);
  setNotesStatus('已抓取当前播放位置 '+fmtTime(t),false);
});
mainAudio.addEventListener('timeupdate',()=>{
  $('current-time').textContent=(mainAudio.currentTime||0).toFixed(1);
});
saveBtn.addEventListener('click', async ()=>{
  if(saveBtn.disabled) return;
  if(!version.sha){
    setNotesStatus('当前音频版本未加载，无法保存；请确认本地反馈服务已启动后刷新页面。',true);
    return;
  }
  const raw=noteTime.value.trim();
  const t=Number(raw);
  const text=noteText.value.trim();
  if(!raw||!Number.isFinite(t)||t<0||t>version.duration){
    setNotesStatus('时间点需为 0 到 '+fmtTime(version.duration)+' 之间的数字。',true);
    noteTime.focus();
    return;
  }
  if(!text){
    setNotesStatus('笔记内容不能为空。',true);
    noteText.focus();
    return;
  }
  // 草稿变化或上次保存成功后换新 request_id；网络重试沿用原 id（服务端幂等去重）
  const sig=draftSignatureOf();
  if(sig!==draftSignature){
    requestId=newRequestId();
    draftSignature=sig;
  }
  saveBtn.disabled=true;
  try{
    const r=await fetch('/api/listening-notes',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      cache:'no-store',
      body:JSON.stringify({run_id:CFG.runId, mix_sha256:version.sha, request_id:requestId,
                           time_seconds:t, category:noteCategory.value, text:text})
    });
    const data=await r.json().catch(()=>({}));
    if(!r.ok){
      const err=new Error(data.error||('HTTP '+r.status));
      err.status=r.status;
      throw err;
    }
    const empty=notesHistory.querySelector('.empty');
    if(empty) empty.remove();
    notesHistory.appendChild(renderNote(data.note));
    noteText.value='';
    requestId=newRequestId();
    draftSignature=null;
    setNotesStatus('笔记已保存。',false);
  }catch(e){
    // 409（音频版本过期或同 request_id 内容冲突）后换新 id；网络错误保留原 id 以便重试
    if(e.status===409){ requestId=newRequestId(); draftSignature=null; }
    setNotesStatus('未保存：'+e.message,true);
  }finally{
    saveBtn.disabled=false;
  }
});

// ---- 既有：分层音量反馈版（/api/revise），行为保持不变 ----
const reviseBtn=$('make');
reviseBtn.addEventListener('click', async ()=>{
  const gains={};
  document.querySelectorAll('[data-track]').forEach(x=>{
    if(+x.value) gains[x.dataset.track]=+x.value;
  });
  const status=$('status');
  if(!Object.keys(gains).length){status.textContent='先选一层要怎么改。';return;}
  reviseBtn.disabled=true; status.textContent='正在渲染新版本…';
  try{
    const r=await fetch('/api/revise',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({run_id:CFG.runId,gains_db:gains})
    });
    const data=await r.json();
    if(!r.ok) throw new Error(data.error||'渲染失败');
    status.textContent=data.reused?'已有同样的反馈版，直接试听。':'新版本已生成，可与上面的原版比较。';
    const result=$('result'); result.replaceChildren();
    const link=document.createElement('a');
    link.href=data.review_url; link.textContent='打开反馈版';
    const audio=document.createElement('audio');
    audio.controls=true;
    audio.src='/media/'+encodeURIComponent(data.review_url.split('/').pop())+'/full_mix.wav';
    result.append(link,audio);
  }catch(e){
    status.textContent='未完成：'+e.message;
  }finally{
    reviseBtn.disabled=false;
  }
});

loadVersion();"""


def build_page(song: Path) -> str:
    manifest = json.loads((song/'run_manifest.json').read_text(encoding='utf-8'))
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
                     for name, label in [('original-phrase.wav', '原创原句'),
                                         ('sample-flip-solo.wav', '翻采层独奏')]
                     if (song/name).is_file())
    sources = render_source_section(song, manifest)
    options = ''.join(f'<option value="{html.escape(code)}">{html.escape(label)}</option>'
                      for code, label in CATEGORY_LABELS)
    cfg = json.dumps({"runId": song.name, "categoryLabels": list(CATEGORY_LABELS)},
                     ensure_ascii=False).replace('<', '\\u003c')
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · BeatLab</title>
<style>{SONG_CSS}</style>
<a href="/">← 所有作品</a><h1>{title}</h1>
<small>{manifest['bpm']} BPM · {manifest['bars']} 小节 · {manifest['duration_seconds']:.1f} 秒 · 待试听</small>
<audio id="main-audio" controls preload="metadata" src="{base}full_mix.wav"></audio>
<section class="notes" aria-labelledby="notes-title">
<h2 id="notes-title">试听笔记</h2>
<p>听到问题或喜欢的地方，标记时间、选类别，再写一句你的感受。</p>
<div id="notes-context" class="ctx">正在读取当前音频版本…</div>
<div class="fld">
  <label for="note-time">时间点（秒）</label>
  <input id="note-time" name="note-time" type="number" min="0" step="0.01" inputmode="decimal" value="0">
  <button id="capture-time" type="button">抓取当前播放位置</button>
  <span>当前 <output id="current-time" for="main-audio">0.0</output> 秒</span>
</div>
<div class="fld">
  <label for="note-category">类别</label>
  <select id="note-category" name="note-category">{options}</select>
</div>
<div class="fld col">
  <label for="note-text">笔记内容（最多 1000 字）</label>
  <textarea id="note-text" name="note-text" maxlength="1000" rows="3"></textarea>
</div>
<button id="save-note" type="button">保存笔记</button>
<p id="notes-status" role="status" aria-live="polite"></p>
<h3>此版本已保存的笔记（点时间跳转）</h3>
<ul id="notes-history" aria-label="当前版本笔记列表"></ul>
<small>笔记保存在当前音频版本下，供下一轮修改参考。旧版本的笔记会保留。</small>
</section>
<details><summary>原句与翻采层试听</summary>{extras}</details>
{sources}
<details><summary>调整声部音量</summary>
<h2>哪一层太抢？</h2><p>选要减弱的层，生成一个可比较的新版本。原版保留，其他层音量保持。</p>
{''.join(rows)}<button id="make">生成反馈版</button><p id="status" role="status"></p><div id="result"></div>
<small>这里只调整指定层音量；不会自动改词、选新采样或重新编曲。Ableton 实际发声与听感另行验收。</small>
</details>
<script>const CFG={cfg};</script>
<script>{PAGE_JS}</script></html>'''
