'use strict';

/*
 * Browser layer: DOM rendering + Web Audio scheduling.
 * Pure pattern/timing logic lives in model.js (window.SimModel).
 */
(function () {
  const M = window.SimModel;

  // ---------------------------------------------------------------- state ----
  const els = {
    errorBanner: document.getElementById('error-banner'),
    lcdTitle: document.getElementById('lcd-title'),
    lcdSource: document.getElementById('lcd-source'),
    lcdDuration: document.getElementById('lcd-duration'),
    lcdGroupSlot: document.getElementById('lcd-group-slot'),
    loadStatus: document.getElementById('load-status'),
    groupBar: document.getElementById('group-bar'),
    padGrid: document.getElementById('pad-grid'),
    play: document.getElementById('btn-play'),
    stop: document.getElementById('btn-stop'),
    bpm: document.getElementById('bpm'),
    bpmVal: document.getElementById('bpm-val'),
    volume: document.getElementById('volume'),
    volumeVal: document.getElementById('volume-val'),
    clear: document.getElementById('btn-clear'),
    example: document.getElementById('btn-example'),
    editorSub: document.getElementById('editor-sub'),
    editor: document.getElementById('editor'),
    rows: document.getElementById('rows')
  };

  const state = {
    manifest: null,
    pads: [],
    padsById: new Map(),
    group: 'A',
    selectedId: null,
    patterns: {},
    bpm: M.DEFAULT_BPM,
    volume: M.DEFAULT_VOLUME,
    ready: false,
    decodedCount: 0,
    playing: false,
    currentStep: -1,
    loadError: null
  };

  const buffers = new Map();   // pad id -> AudioBuffer
  const failedIds = new Set(); // pads whose WAV could not be fetched/decoded

  // Audio engine.
  let ctx = null;
  let master = null;
  let loadPromise = null;
  // Generation token: Stop() bumps it; any async load/start scheduled
  // before the bump must observe the new value and bail out.
  let generation = 0;
  let schedulerTimer = null;
  let nextStepTime = 0;
  let stepCursor = 0;
  const scheduledEvents = [];          // {step,time,ids,remaining}
  const voices = new Map();            // BufferSource -> GainNode (incl. auditions)
  const visualTimers = new Set();

  // ------------------------------------------------------------ helpers ------
  function h(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function errMessage(err) {
    return err && err.message ? err.message : String(err);
  }

  function positionForSlot(slot) {
    const layout = M.physicalLayout();
    for (let i = 0; i < layout.length; i++) {
      if (layout[i].slot === slot) return layout[i];
    }
    return null;
  }

  function selectedPad() {
    return state.selectedId ? state.padsById.get(state.selectedId) || null : null;
  }

  function setLoadStatus(text) {
    els.loadStatus.textContent = text;
  }

  function showError(message) {
    if (message) {
      els.errorBanner.textContent = message;
      els.errorBanner.hidden = false;
    } else {
      els.errorBanner.textContent = '';
      els.errorBanner.hidden = true;
    }
  }

  function isUsablePad(pad) {
    return pad && typeof pad.id === 'string' && pad.id.length > 0 &&
           typeof pad.file === 'string';
  }

  // ----------------------------------------------------------- manifest ------
  function setupManifest(manifest) {
    if (!manifest || !Array.isArray(manifest.pads)) {
      throw new Error('set.json 缺少 pads 列表');
    }
    state.manifest = manifest;
    state.pads = manifest.pads.filter(isUsablePad);
    state.pads.forEach(function (pad) { state.padsById.set(pad.id, pad); });

    state.bpm = M.clampBpm(manifest.bpm);
    state.volume = M.DEFAULT_VOLUME;
    state.patterns = M.loadExample(state.pads);

    els.bpm.value = String(state.bpm);
    els.bpmVal.textContent = String(state.bpm);
    els.bpm.disabled = false;
    els.volume.value = String(state.volume);
    els.volumeVal.textContent = state.volume.toFixed(2);
    els.volume.disabled = false;
    els.example.disabled = false;

    paintGroupBar();
    renderPads();
    renderRows();
    paintTransport();

    const first = M.orderedPads(state.pads)[0];
    if (first) selectPad(first.id);

    setLoadStatus('点击鼓垫试听，或按播放载入全部 ' + state.pads.length + ' 个采样（不会自动播放）');
  }

  // ------------------------------------------------------------ web audio ----
  // Must be invoked synchronously from a user gesture so resume() counts.
  function gestureAudio() {
    if (state.loadError && !ctx) {
      // A hard failure happened before the context existed; keep controls honest.
      return Promise.resolve(false);
    }
    if (!ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) {
        state.loadError = '当前浏览器不支持 Web Audio。';
        showError(state.loadError);
        paintTransport();
        return Promise.resolve(false);
      }
      ctx = new AC();
      master = ctx.createGain();
      master.gain.value = state.volume * M.MASTER_HEADROOM;
      master.connect(ctx.destination);
    }
    return Promise.resolve(ctx.state === 'suspended' ? ctx.resume() : undefined)
      .then(loadAllSamples);
  }

  // Fetch + decode every sample exactly once, with visible progress.
  function loadAllSamples() {
    if (loadPromise) return loadPromise;
    const total = state.pads.length;
    state.ready = false;
    setLoadStatus('解码采样 0/' + total + '…');
    let done = 0;
    let success = 0;

    loadPromise = (async function () {
      for (const pad of state.pads) {
        try {
          const res = await fetch(pad.file);
          if (!res.ok) throw new Error('HTTP ' + res.status);
          const raw = await res.arrayBuffer();
          const audio = await ctx.decodeAudioData(raw);
          buffers.set(pad.id, audio);
          success += 1;
        } catch (err) {
          failedIds.add(pad.id);
        }
        done += 1;
        state.decodedCount = success;
        setLoadStatus('解码采样 ' + done + '/' + total + '…');
        renderPads();
      }
      if (failedIds.size > 0) {
        state.ready = false;
        state.loadError = '有 ' + failedIds.size + ' 个采样缺失或无法解码，播放已禁用（不会静默跳过）。';
        showError(state.loadError);
        setLoadStatus('采样错误：' + success + '/' + total + ' 可用');
      } else {
        state.ready = true;
        state.loadError = null;
        showError(null);
        setLoadStatus('采样就绪 ' + success + '/' + total);
      }
      paintTransport();
      renderPads();
      return state.ready;
    })();

    return loadPromise;
  }

  // Existing set WAVs already contain their documented gain_db; do not apply twice.
  function startVoice(pad, when) {
    if (!ctx || !master || !pad) return null;
    const buffer = buffers.get(pad.id);
    if (!buffer) return null;

    const src = ctx.createBufferSource();
    src.buffer = buffer;
    const gain = ctx.createGain();
    gain.gain.value = 1;
    src.connect(gain);
    gain.connect(master);

    src.onended = function () {
      voices.delete(src);
      src.disconnect();
      try { gain.disconnect(); } catch (e) { /* node already gone */ }
    };

    if (when > 0) src.start(when);
    else src.start(); // audition: now
    voices.set(src, gain);
    return src;
  }

  // ----------------------------------------------------------- playback ------
  function startPlayback() {
    const gen = generation;
    state.playing = true;
    state.currentStep = -1;
    stepCursor = 0;
    nextStepTime = ctx.currentTime + 0.06;
    paintTransport();
    paintStep();
    schedulerTick(gen);
    schedulerTimer = setInterval(function () { schedulerTick(gen); }, M.SCHED_INTERVAL_MS);
  }

  function stopPlayback() {
    generation += 1; // cancel anything asynchronous still in flight

    if (schedulerTimer !== null) {
      clearInterval(schedulerTimer);
      schedulerTimer = null;
    }
    visualTimers.forEach(function (timer) { clearTimeout(timer); });
    visualTimers.clear();

    // Stop ALL active/scheduled sources, including ringing auditions.
    Array.from(voices.entries()).forEach(function (entry) {
      const src = entry[0];
      const gain = entry[1];
      try { src.onended = null; src.stop(); src.disconnect(); } catch (e) { /* already stopped */ }
      try { gain.disconnect(); } catch (e) { /* noop */ }
    });
    voices.clear();
    scheduledEvents.length = 0;

    state.playing = false;
    state.currentStep = -1;
    paintTransport();
    paintStep();
  }

  function schedulerTick(gen) {
    if (gen !== generation || !ctx) return;
    while (nextStepTime < ctx.currentTime + M.SCHED_LOOKAHEAD_S) {
      scheduleStep(stepCursor, nextStepTime, gen);
      nextStepTime += M.stepDurationSeconds(state.bpm); // live BPM changes apply
      stepCursor = (stepCursor + 1) % M.STEP_COUNT;
    }
  }

  function scheduleStep(step, when, gen) {
    const ids = Object.keys(state.patterns).filter(function (id) {
      const row = state.patterns[id];
      return row && row[step] && buffers.has(id);
    });

    let ev = null;
    if (ids.length > 0) {
      ev = { step: step, time: when, ids: ids.slice(), remaining: ids.length };
      scheduledEvents.push(ev);
      ids.forEach(function (id) {
        const src = startVoice(state.padsById.get(id), when);
        if (!src) {
          ev.remaining -= 1;
          return;
        }
        src.addEventListener('ended', function () {
          ev.remaining -= 1;
          const i = scheduledEvents.indexOf(ev);
          if (ev.remaining <= 0 && i >= 0) scheduledEvents.splice(i, 1);
        });
      });
      if (ev.remaining <= 0) {
        const i = scheduledEvents.indexOf(ev);
        if (i >= 0) scheduledEvents.splice(i, 1);
      }
    }

    // Visual highlight is timed against AudioContext.currentTime.
    const delay = Math.max(0, (when - ctx.currentTime) * 1000);
    const timer = setTimeout(function () {
      visualTimers.delete(timer);
      if (gen !== generation) return; // Stop cancelled this queued visual
      state.currentStep = step;
      const i = ev ? scheduledEvents.indexOf(ev) : -1;
      if (i >= 0) scheduledEvents.splice(i, 1);
      paintStep();
    }, delay);
    visualTimers.add(timer);
  }

  // -------------------------------------------------------------- render -----
  function paintGroupBar() {
    els.groupBar.querySelectorAll('.group-btn').forEach(function (btn) {
      btn.classList.toggle('active', btn.dataset.group === state.group);
      btn.setAttribute('aria-selected',String(btn.dataset.group === state.group));
    });
  }

  function renderGroupBar() {
    els.groupBar.textContent = '';
    M.GROUPS.forEach(function (group) {
      const btn = h('button', 'group-btn', group + '  ' + M.GROUP_TITLES[group]);
      btn.type = 'button';
      btn.dataset.group = group;
      btn.setAttribute('role', 'tab');
      btn.addEventListener('click', function () {
        state.group = group; // switching groups never touches patterns
        const first = M.orderedPads(state.pads).find(pad => pad.group === group);
        if (first) selectPad(first.id, true);
        else { paintGroupBar(); renderPads(); }
      });
      els.groupBar.appendChild(btn);
    });
  }

  function renderPads() {
    els.padGrid.textContent = '';
    M.physicalLayout().forEach(function (pos) {
      const pad = M.findPad(state.pads, state.group, pos.slot);
      const btn = h('button', 'pad');
      btn.type = 'button';

      const key = h('span', 'pad-key' + (pos.label.length > 1 ? ' wide' : ''), pos.label);
      btn.appendChild(key);

      if (!pad) {
        btn.classList.add('empty');
        btn.disabled = true;
        btn.appendChild(h('span', 'pad-name', '—'));
      } else {
        btn.dataset.padId = pad.id;
        if (failedIds.has(pad.id)) btn.classList.add('failed');
        if (pad.id === state.selectedId) btn.classList.add('selected');
        btn.appendChild(h('span', 'pad-name', pad.label || pad.id));
        btn.addEventListener('click', function () { onPadClick(pad); });
      }
      els.padGrid.appendChild(btn);
    });
  }

  function onPadClick(pad) {
    selectPad(pad.id, false);
    if (failedIds.has(pad.id)) {
      setLoadStatus('采样缺失，无法试听：' + pad.file);
      return;
    }
    const gen = generation;
    gestureAudio().then(function (ok) {
      // Async load/start must not fire after Stop, nor for a different choice.
      if (ok && gen === generation && state.selectedId === pad.id) {
        startVoice(pad, 0);
      }
    }).catch(function (err) { showError('试听启动失败：' + errMessage(err)); });
  }

  function selectPad(id) {
    const pad = state.padsById.get(id);
    if (!pad) return;
    state.selectedId = id;
    state.group = pad.group; // follow the pad across groups, never reset patterns
    paintGroupBar();
    renderPads();
    renderLcd();
    renderEditor();
    renderRows();
    paintTransport();
  }

  function renderLcd() {
    const pad = selectedPad();
    if (!pad) {
      els.lcdTitle.textContent = '未选择鼓垫';
      els.lcdSource.textContent = '';
      els.lcdDuration.textContent = '';
      els.lcdGroupSlot.textContent = '';
      return;
    }
    els.lcdTitle.textContent = pad.label || pad.id;
    const prov = pad.provenance || {};
    const sourceParts = [];
    if (prov.source_title) sourceParts.push('来源：' + prov.source_title);
    if (prov.note) sourceParts.push(prov.note);
    els.lcdSource.textContent = sourceParts.join('　｜　');
    els.lcdDuration.textContent = Number.isFinite(pad.duration_seconds)
      ? '时长 ' + pad.duration_seconds.toFixed(2) + ' 秒'
      : '时长 —';
    const pos = positionForSlot(pad.slot);
    els.lcdGroupSlot.textContent = '组 ' + pad.group + ' · 物理键 ' + (pos ? pos.label : pad.slot);
  }

  function renderEditor() {
    els.editor.textContent = '';
    const pad = selectedPad();
    const row = pad ? state.patterns[pad.id] : null;

    if (!pad) {
      els.editorSub.textContent = '浏览器练习格 · 非设备操作方式';
    } else {
      els.editorSub.textContent = '为「' + (pad.label || pad.id) + '」点亮格子 · 非设备操作方式';
    }

    for (let beat = 0; beat < M.BEAT_COUNT; beat++) {
      const beatEl = h('div', 'beat');
      for (let k = 0; k < M.STEPS_PER_BEAT; k++) {
        const step = beat * M.STEPS_PER_BEAT + k;
        const cell = h('button', 'step-cell');
        cell.type = 'button';
        cell.dataset.step = String(step);
        cell.disabled = !pad;
        cell.setAttribute('aria-label','第 '+(step+1)+' 步');
        cell.setAttribute('aria-pressed',String(!!(row && row[step])));
        if (row && row[step]) cell.classList.add('on');
        if (step === state.currentStep) cell.classList.add('current');
        cell.appendChild(h('span', 'step-num', String(step + 1)));
        cell.addEventListener('click', function () {
          if (!state.selectedId) return;
          state.patterns = M.toggleStep(state.patterns, state.selectedId, step);
          renderEditor();
          renderRows();
          paintStep();
        });
        beatEl.appendChild(cell);
      }
      els.editor.appendChild(beatEl);
    }
  }

  function renderRows() {
    els.rows.textContent = '';
    const active = M.orderedPads(state.pads).filter(function (pad) {
      return M.hasActiveSteps(state.patterns, pad.id);
    });
    if (active.length === 0) {
      els.rows.appendChild(h('p', 'rows-empty', '还没有点亮的格子：选一个鼓垫开始。'));
      return;
    }
    active.forEach(function (pad) {
      const row = h('button', 'pattern-row');
      row.type = 'button';
      if (pad.id === state.selectedId) row.classList.add('selected');
      const pos = positionForSlot(pad.slot);
      row.appendChild(h('span', 'row-name',
        pad.group + '·' + (pos ? pos.label : pad.slot) + ' ' + (pad.label || pad.id)));

      const strip = h('span', 'row-strip');
      for (let step = 0; step < M.STEP_COUNT; step++) {
        const dot = h('span', 'step-cell dot' + (state.patterns[pad.id][step] ? ' on' : ''));
        dot.dataset.step = String(step);
        if (step === state.currentStep) dot.classList.add('current');
        strip.appendChild(dot);
      }
      row.appendChild(strip);
      row.addEventListener('click', function () { selectPad(pad.id); });
      els.rows.appendChild(row);
    });
  }

  // Repaint only the running-step highlight (no rebuild while scheduling).
  function paintStep() {
    document.querySelectorAll('.step-cell').forEach(function (cell) {
      cell.classList.toggle('current', Number(cell.dataset.step) === state.currentStep);
    });
  }

  function paintTransport() {
    els.play.disabled = !state.manifest || state.playing || !!state.loadError;
    els.stop.disabled = !state.manifest;
    els.clear.disabled = !state.selectedId;
  }

  // ------------------------------------------------------------- controls ----
  function bindStaticControls() {
    renderGroupBar();

    els.play.addEventListener('click', function () {
      if (state.playing || state.loadError) return;
      const gen = generation;
      Promise.resolve(gestureAudio()).then(function (ok) {
        if (ok && gen === generation && !state.playing && !state.loadError) {
          startPlayback();
        }
      }).catch(function (err) {
        showError('播放启动失败：' + errMessage(err));
      });
    });

    els.stop.addEventListener('click', stopPlayback);

    els.bpm.addEventListener('input', function () {
      state.bpm = M.clampBpm(els.bpm.value);
      els.bpmVal.textContent = String(state.bpm);
    });

    els.volume.addEventListener('input', function () {
      const v = Number(els.volume.value);
      state.volume = Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : M.DEFAULT_VOLUME;
      els.volumeVal.textContent = state.volume.toFixed(2);
      if (master && ctx) {
        master.gain.setTargetAtTime(state.volume * M.MASTER_HEADROOM, ctx.currentTime, 0.02);
      }
    });

    els.clear.addEventListener('click', function () {
      if (!state.selectedId) return;
      state.patterns = M.clearPattern(state.patterns, state.selectedId);
      renderEditor();
      renderRows();
    });

    els.example.addEventListener('click', function () {
      // Explicitly replaces the current pattern, as labelled.
      state.patterns = M.loadExample(state.pads);
      renderEditor();
      renderRows();
      paintStep();
    });

    document.addEventListener('visibilitychange', function () {
      if (document.hidden && ctx) stopPlayback();
    });
  }

  // ---------------------------------------------------------------- init -----
  function init() {
    bindStaticControls();
    paintTransport();
    fetch('set.json')
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(setupManifest)
      .catch(function (err) {
        showError('无法读取 set.json：' + errMessage(err) + '。请通过本地静态服务器打开本页。');
        setLoadStatus('set.json 读取失败');
      });
  }

  // Read-only diagnostic snapshot for parent browser tests. Each call
  // returns a fresh copy; nothing here mutates simulator state.
  function snapshot() {
    return {
      ready: state.ready,
      decodedCount: state.decodedCount,
      playing: state.playing,
      currentStep: state.currentStep,
      activeVoices: voices.size,
      group: state.group,
      selectedId: state.selectedId,
      patterns: M.clonePatterns(state.patterns),
      scheduledEvents: scheduledEvents.map(function (ev) {
        return { step: ev.step, time: ev.time, ids: ev.ids.slice() };
      })
    };
  }

  Object.defineProperty(window, 'simulator', {
    enumerable: true,
    configurable: false,
    writable: false,
    value: Object.freeze({ snapshot: snapshot })
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
