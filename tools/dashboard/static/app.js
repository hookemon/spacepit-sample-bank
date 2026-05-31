// spacepit capture dashboard — frontend logic
(() => {
  const els = {
    instrumentSelect: document.getElementById('instrument-select'),
    progressFill: document.getElementById('progress-fill'),
    progressPct: document.getElementById('progress-pct'),
    progressTier: document.getElementById('progress-tier'),
    checklist: document.getElementById('checklist'),
    styleButtons: document.querySelectorAll('.styles button'),
    styleForms: document.querySelectorAll('.style-form'),
    captureBtn: document.getElementById('capture-btn'),
    status: document.getElementById('status'),
    consoleCard: document.getElementById('console-card'),
    consoleEl: document.getElementById('console'),
  };

  let currentStyle = 'multisample';
  let lastCapture = null;  // { wav_path, wav_url, params }
  let audioContext = null;
  // slug -> real preset display name (e.g. "sub-bass" -> "A14 · Juno Sub Bass"), populated
  // from the patch manifest so the captures log shows real names, not under-the-hood slugs.
  const presetNames = {};
  const realName = (slug) => presetNames[slug] || slug;

  // ---------- audio settings — discovered devices + per-instrument storage ----------
  let availableDevices = { midi_ports: [], audio_devices: [] };

  function getAudioSettings() {
    return {
      audio_device: document.getElementById('set-audio-device')?.value.trim() || '',
      input_channels: document.getElementById('set-input-channels')?.value.trim() || '1,2',
      midi_port: document.getElementById('set-midi-port')?.value.trim() || '',
    };
  }

  function populateChannelsDropdown(channelCount) {
    const sel = document.getElementById('set-input-channels-select');
    const prev = document.getElementById('set-input-channels').value;
    let html = '<option value="">— pick or type custom —</option>';
    // Stereo pairs first (most common for synths)
    for (let i = 1; i <= channelCount; i += 2) {
      if (i + 1 <= channelCount) {
        html += `<option value="${i},${i+1}">${i}+${i+1} (stereo pair)</option>`;
      }
    }
    // Then individual mono channels
    html += '<option disabled>──── mono ────</option>';
    for (let i = 1; i <= channelCount; i++) {
      html += `<option value="${i}">${i} (mono)</option>`;
    }
    sel.innerHTML = html;
    // Sync select with the text input value
    if (prev) sel.value = prev;
  }

  async function refreshDevices() {
    isLoadingSettings = true;  // don't save during dropdown rebuild
    try {
      const r = await fetch('/api/devices');
      availableDevices = await r.json();
      const audioSel = document.getElementById('set-audio-device');
      const midiSel = document.getElementById('set-midi-port');
      const prevAudio = audioSel.value;
      const prevMidi = midiSel.value;
      audioSel.innerHTML = '<option value="">— select audio device —</option>' +
        availableDevices.audio_devices.map(d => `<option value="${d.name}" data-channels="${d.channels}">${d.name} (${d.channels} ch)</option>`).join('');
      midiSel.innerHTML = '<option value="">— select MIDI port —</option>' +
        availableDevices.midi_ports.map(p => `<option value="${p}">${p}</option>`).join('');
      if (prevAudio) audioSel.value = prevAudio;
      if (prevMidi) midiSel.value = prevMidi;

      // Populate channels dropdown based on selected device's channel count
      const updateChannels = () => {
        const selOpt = audioSel.options[audioSel.selectedIndex];
        const channels = parseInt(selOpt?.dataset?.channels) || 2;
        populateChannelsDropdown(channels);
      };
      audioSel.removeEventListener('change', updateChannels);
      audioSel.addEventListener('change', updateChannels);
      updateChannels();
    } catch (e) {
      console.error('failed to load devices', e);
    } finally {
      setTimeout(() => { isLoadingSettings = false; }, 200);
    }
  }

  // Wire the channels-dropdown → text-input sync
  document.addEventListener('DOMContentLoaded', () => {
    const sel = document.getElementById('set-input-channels-select');
    const txt = document.getElementById('set-input-channels');
    if (sel && txt) {
      sel.addEventListener('change', () => {
        if (sel.value) {
          txt.value = sel.value;
          saveInstrumentSettings();
        }
      });
      txt.addEventListener('input', () => {
        // try to mirror in dropdown if it exists
        const match = Array.from(sel.options).find(o => o.value === txt.value);
        if (match) sel.value = txt.value;
      });
    }
  });

  // Per-instrument settings — load on instrument change, save on field change
  async function loadInstrumentSettings(slug) {
    isLoadingSettings = true;  // guard — block any auto-save during this load
    try {
      const r = await fetch(`/api/instruments/${slug}/settings`);
      if (!r.ok) return;
      const data = await r.json();
      const s = data.capture_settings || data;
      // Auto-select the saved values. Important: do AUDIO device FIRST so the channels
      // dropdown gets rebuilt against the right channel count, THEN set channels.
      if (s.audio_device) {
        const audioSel = document.getElementById('set-audio-device');
        // Confirm the device exists in the dropdown options before setting
        const audioOpt = Array.from(audioSel.options).find(o => o.value === s.audio_device);
        if (audioOpt) {
          audioSel.value = s.audio_device;
          // Dispatch change to trigger channels-dropdown rebuild
          audioSel.dispatchEvent(new Event('change'));
        } else {
          console.warn(`audio device "${s.audio_device}" not in dropdown — leaving as-is`);
        }
      }
      // Now set channels — the channels dropdown was just rebuilt above
      if (s.input_channels) {
        document.getElementById('set-input-channels').value = s.input_channels;
        const chSel = document.getElementById('set-input-channels-select');
        if (chSel) {
          const match = Array.from(chSel.options).find(o => o.value === s.input_channels);
          if (match) {
            chSel.value = s.input_channels;
          } else {
            // Channel value not in current dropdown options — leave text input as source of truth
            chSel.value = '';
          }
        }
      }
      // MIDI routing graph: prefer the RESOLVED port (live-checked against available ports),
      // not the manifest's stored value. This way if you plug the JP-8000 in direct USB,
      // it auto-picks "Roland JP-8000" instead of staying on the saved "mio".
      const resolvedPort = data.resolved_port || s.midi_port;
      if (resolvedPort) document.getElementById('set-midi-port').value = resolvedPort;
      // Update the routing-graph hint so user sees "synth → port" relationship
      const routingHint = document.getElementById('midi-routing-hint');
      if (routingHint) {
        if (data.resolved_via === 'synth_name') {
          routingHint.innerHTML = `🟢 <b>${data.synth_name}</b> recognized directly`;
          routingHint.style.color = 'var(--green, #5fb866)';
        } else if (data.resolved_via === 'saved_port') {
          routingHint.innerHTML = `🟡 <b>${data.synth_name}</b> → <b>${resolvedPort}</b> (routed through interface)`;
          routingHint.style.color = 'var(--amber)';
        } else {
          routingHint.innerHTML = `🔴 <b>${data.synth_name || 'this synth'}</b> not connected — plug it in or pick a port`;
          routingHint.style.color = '#e88';
        }
      }
      const polyphony = data.polyphony || 'unknown';
      const polySel = document.getElementById('set-polyphony');
      if (polySel) polySel.value = polyphony;
      updatePolyphonyHints(polyphony);
    } catch (e) {}
    finally {
      // small delay to absorb any straggling change events from the dropdowns
      setTimeout(() => {
        isLoadingSettings = false;
        // Re-lock the clock to this instrument's MIDI port (no-op if already locked correctly).
        // autoStartClock may not exist yet on the very first call — guard with typeof.
        if (typeof autoStartClock === 'function') autoStartClock();
      }, 250);
    }
  }

  function updatePolyphonyHints(polyphony) {
    const hint = document.getElementById('poly-hint');
    const warning = document.getElementById('prog-mode-warning');
    const sendMode = document.getElementById('prog-send-mode');
    if (!hint || !warning || !sendMode) return;

    if (polyphony === 'monophonic') {
      hint.textContent = ` · instrument is MONOPHONIC`;
      hint.style.color = 'var(--amber)';
      // Default to "arp" mode for mono synths — sequences chord notes one at a time
      if (sendMode.value === 'chord') {
        warning.style.display = 'block';
        warning.innerHTML = `⚠ This synth is monophonic. "chord" mode will only play ONE note per chord change (the synth picks based on note priority). For real chord progressions, either: (1) engage the synth's ARP and keep "chord" mode, (2) pick "arp" mode for script-side arpeggio, or (3) pick "root only" for a bass line following the chord roots.`;
      } else {
        warning.style.display = 'none';
      }
    } else if (polyphony === 'polyphonic') {
      hint.textContent = ` · instrument is POLYPHONIC`;
      hint.style.color = 'var(--green)';
      warning.style.display = 'none';
    } else {
      hint.textContent = ' · (polyphony unknown — set in audio routing panel)';
      hint.style.color = 'var(--fg-faint)';
      warning.style.display = 'none';
    }
  }

  // Re-evaluate warning when send mode or polyphony changes
  document.getElementById('prog-send-mode').addEventListener('change', () => {
    const poly = document.getElementById('set-polyphony').value;
    updatePolyphonyHints(poly);
  });
  document.getElementById('set-polyphony').addEventListener('change', () => {
    const poly = document.getElementById('set-polyphony').value;
    updatePolyphonyHints(poly);
    saveInstrumentSettings();
  });

  let isLoadingSettings = false;  // guard — don't save during programmatic updates

  async function saveInstrumentSettings() {
    if (isLoadingSettings) return;  // skip auto-save while loading/refreshing
    const slug = document.getElementById('instrument-select').value;
    if (!slug) return;
    const settings = getAudioSettings();
    const polyphony = document.getElementById('set-polyphony')?.value;
    // Don't write an empty/unset string if the field WAS empty for a programmatic reason
    if (!settings.midi_port && !settings.audio_device && !settings.input_channels) {
      return;  // all empty = page is mid-load, skip
    }
    try {
      await fetch(`/api/instruments/${slug}/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...settings, polyphony }),
      });
    } catch (e) {}
  }

  ['set-audio-device', 'set-input-channels', 'set-midi-port'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', saveInstrumentSettings);
  });

  document.getElementById('refresh-devices-btn').addEventListener('click', refreshDevices);

  // ---------- waveform rendering ----------
  function ensureAudioContext() {
    if (!audioContext) audioContext = new (window.AudioContext || window.webkitAudioContext)();
    return audioContext;
  }

  // Simple 2-pole biquad filter (low-pass / high-pass) — for the 3-band split
  function makeBiquad(type, freq, sr) {
    const w0 = 2 * Math.PI * freq / sr;
    const cosw = Math.cos(w0), sinw = Math.sin(w0);
    const Q = 0.707;
    const alpha = sinw / (2 * Q);
    let b0, b1, b2, a0, a1, a2;
    if (type === 'lp') {
      b0 = (1 - cosw) / 2; b1 = 1 - cosw; b2 = (1 - cosw) / 2;
      a0 = 1 + alpha; a1 = -2 * cosw; a2 = 1 - alpha;
    } else { // hp
      b0 = (1 + cosw) / 2; b1 = -(1 + cosw); b2 = (1 + cosw) / 2;
      a0 = 1 + alpha; a1 = -2 * cosw; a2 = 1 - alpha;
    }
    return { b0: b0/a0, b1: b1/a0, b2: b2/a0, a1: a1/a0, a2: a2/a0 };
  }

  function applyBiquad(input, coeff) {
    const out = new Float32Array(input.length);
    let x1 = 0, x2 = 0, y1 = 0, y2 = 0;
    for (let i = 0; i < input.length; i++) {
      const x0 = input[i];
      const y0 = coeff.b0*x0 + coeff.b1*x1 + coeff.b2*x2 - coeff.a1*y1 - coeff.a2*y2;
      out[i] = y0;
      x2 = x1; x1 = x0; y2 = y1; y1 = y0;
    }
    return out;
  }

  function bandPass(input, lowHz, highHz, sr) {
    // simple bandpass = lowpass at highHz then highpass at lowHz
    let s = applyBiquad(input, makeBiquad('lp', highHz, sr));
    s = applyBiquad(s, makeBiquad('hp', lowHz, sr));
    return s;
  }

  async function drawWaveform(url) {
    const canvas = document.getElementById('waveform');
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    ctx.fillStyle = '#050505';
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = '#888';
    ctx.font = '11px monospace';
    ctx.fillText('decoding…', 8, 16);

    try {
      const ac = ensureAudioContext();
      const resp = await fetch(url);
      const buf = await resp.arrayBuffer();
      const audio = await ac.decodeAudioData(buf);
      const sr = audio.sampleRate;
      const channel = audio.numberOfChannels > 1 ? mixToMono(audio) : audio.getChannelData(0);

      // Filter into 3 bands (Serato-style)
      ctx.fillText('filtering bands…', 8, 32);
      const lowBand  = applyBiquad(channel, makeBiquad('lp', 250, sr));
      const midBand  = bandPass(channel, 250, 4000, sr);
      const highBand = applyBiquad(channel, makeBiquad('hp', 4000, sr));

      // peak per pixel column for each band
      const samplesPerPx = Math.ceil(channel.length / W);
      const peakBand = (band) => {
        const peaks = new Array(W);
        for (let x = 0; x < W; x++) {
          let max = 0;
          const start = x * samplesPerPx;
          const end = Math.min(band.length, start + samplesPerPx);
          for (let i = start; i < end; i++) {
            const a = Math.abs(band[i]);
            if (a > max) max = a;
          }
          peaks[x] = max;
        }
        return peaks;
      };
      const peaksLow  = peakBand(lowBand);
      const peaksMid  = peakBand(midBand);
      const peaksHigh = peakBand(highBand);

      // global peak from all bands for normalization
      let gp = 0;
      for (let x = 0; x < W; x++) {
        gp = Math.max(gp, peaksLow[x] + peaksMid[x] + peaksHigh[x]);
      }
      if (gp === 0) gp = 1;

      // redraw clean
      ctx.fillStyle = '#050505';
      ctx.fillRect(0, 0, W, H);
      // zero line
      ctx.fillStyle = '#1a1a1a';
      ctx.fillRect(0, H/2, W, 1);

      // Stacked 3-band waveform — Serato style
      // BLUE = low, GREEN = mid, AMBER = high
      const COLORS = {
        low:  '#3a9eff',
        mid:  '#5fb866',
        high: '#F2B705',
      };
      const maxH = H / 2 - 2;
      for (let x = 0; x < W; x++) {
        const lo = peaksLow[x] / gp;
        const md = peaksMid[x] / gp;
        const hi = peaksHigh[x] / gp;
        // total stacked height
        const total = (lo + md + hi) * maxH;
        // stack from inside out: high in middle, mid wraps, low outermost
        // (gives a Serato-like layered look)
        const hiH = hi * maxH;
        const mdH = md * maxH;
        const loH = lo * maxH;

        // High (amber) — center
        ctx.fillStyle = COLORS.high;
        ctx.fillRect(x, H/2 - hiH, 1, hiH * 2);
        // Mid (green) — outside high
        ctx.fillStyle = COLORS.mid;
        ctx.fillRect(x, H/2 - hiH - mdH, 1, mdH);
        ctx.fillRect(x, H/2 + hiH, 1, mdH);
        // Low (blue) — outermost
        ctx.fillStyle = COLORS.low;
        ctx.fillRect(x, H/2 - hiH - mdH - loH, 1, loH);
        ctx.fillRect(x, H/2 + hiH + mdH, 1, loH);
      }

      // mini legend in top-left
      ctx.font = '9px monospace';
      ctx.fillStyle = COLORS.low; ctx.fillText('● LOW', 6, 12);
      ctx.fillStyle = COLORS.mid; ctx.fillText('● MID', 50, 12);
      ctx.fillStyle = COLORS.high; ctx.fillText('● HIGH', 92, 12);

      // stats
      const globalPeak = Math.max(...peaksLow.map((_, i) => peaksLow[i] + peaksMid[i] + peaksHigh[i]));
      const truePeak = Math.max(...Array.from(channel, Math.abs));
      const peakDb = truePeak > 0 ? (20 * Math.log10(truePeak)).toFixed(1) : '-inf';
      const dur = audio.duration.toFixed(2);
      document.getElementById('wave-stats').textContent =
        `${dur}s · peak ${peakDb} dBFS · ${audio.sampleRate} Hz · ${audio.numberOfChannels}ch  ·  3-band view: low (≤250Hz) / mid (250Hz-4kHz) / high (≥4kHz)`;
    } catch (e) {
      ctx.fillStyle = '#d65656';
      ctx.fillText('failed to decode: ' + e.message, 8, 32);
      document.getElementById('wave-stats').textContent = '';
    }
  }

  function mixToMono(audio) {
    const len = audio.length;
    const out = new Float32Array(len);
    for (let ch = 0; ch < audio.numberOfChannels; ch++) {
      const data = audio.getChannelData(ch);
      for (let i = 0; i < len; i++) out[i] += data[i];
    }
    for (let i = 0; i < len; i++) out[i] /= audio.numberOfChannels;
    return out;
  }

  // ---------- instrument loading ----------
  // Persist the selected instrument across page refreshes so you don't lose your
  // place every time you reload. localStorage key: 'bench.lastInstrument'
  async function loadInstruments() {
    const r = await fetch('/api/instruments');
    const instruments = await r.json();
    els.instrumentSelect.innerHTML = '';
    for (const i of instruments) {
      const opt = document.createElement('option');
      opt.value = i.slug;
      opt.textContent = `${i.name} (${i.slug})`;
      els.instrumentSelect.appendChild(opt);
    }
    if (instruments.length === 0) return;

    // Restore last-selected instrument from localStorage, fall back to first
    let preferredSlug = null;
    try { preferredSlug = localStorage.getItem('bench.lastInstrument'); } catch (e) {}
    let preferred = preferredSlug ? instruments.find(i => i.slug === preferredSlug) : null;
    if (!preferred) preferred = instruments[0];

    els.instrumentSelect.value = preferred.slug;
    renderInstrument(preferred);
    loadInstrumentSettings(preferred.slug);
    loadIconicPatches(preferred.slug);
  }

  async function loadInstrument(slug) {
    const r = await fetch(`/api/instruments/${slug}`);
    if (!r.ok) return;
    const data = await r.json();
    renderInstrument(data);
  }

  function renderInstrument(i) {
    const t = i.targets || {};
    // captured patches = those with at least one WAV in any chain (NOT total slots).
    // (this was showing patch_count/target e.g. "12/11" — wrong on both numbers)
    const capturedPatches = (i.patches || []).filter(p => Object.values(p.chains || {}).some(v => v > 0)).length;
    const totalPatches = i.patch_count || (i.patches || []).length || 1;
    const patchPct = Math.min(100, (capturedPatches / totalPatches) * 100);
    const loopPct = Math.min(100, (i.loops_count / (t.loops || 1)) * 100);
    const sweepPct = Math.min(100, (i.sweeps_count / (t.sweeps || 1)) * 100);
    const photoPct = Math.min(100, (i.photos_count / (t.photos || 1)) * 100);
    const overall = Math.round((patchPct + loopPct + sweepPct + photoPct) / 4);

    els.progressFill.style.width = `${overall}%`;
    els.progressPct.textContent = `${overall}%`;
    els.progressTier.textContent = i.tier || '—';

    const checklist = [
      { label: 'Patches multisampled', current: capturedPatches, target: totalPatches },
      { label: 'Loops captured', current: i.loops_count, target: t.loops },
      { label: 'Sweeps captured', current: i.sweeps_count, target: t.sweeps },
      { label: 'Photos', current: i.photos_count, target: t.photos },
    ];
    els.checklist.innerHTML = '<div class="checklist-section">' + checklist.map(c => {
      const cls = c.current >= c.target ? 'done' : (c.current > 0 ? 'partial' : 'todo');
      return `<div class="check ${cls}"><span>${c.label}</span><span class="num">${c.current}/${c.target}</span></div>`;
    }).join('') + '</div>';

    // Patches detail
    if (i.patches && i.patches.length > 0) {
      const patchHtml = i.patches.map(p => {
        const chains = Object.entries(p.chains || {}).map(([k, v]) => `${k}:${v}`).join(' · ');
        return `<div class="check done" style="font-size: 10px;"><span>${p.name}</span><span class="num" style="font-size: 9px;">${chains}</span></div>`;
      }).join('');
      els.checklist.innerHTML += `<h2 style="margin-top: 16px;">Patches</h2><div class="checklist-section">${patchHtml}</div>`;
    }
  }

  // ---------- style switching ----------
  els.styleButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      els.styleButtons.forEach(b => b.classList.remove('selected'));
      btn.classList.add('selected');
      currentStyle = btn.dataset.style;
      els.styleForms.forEach(f => f.classList.toggle('active', f.dataset.form === currentStyle));
      // Hide compose card when in multisample mode — multisampling needs focus, not clutter
      const composeCard = document.getElementById('compose-card');
      if (composeCard) composeCard.style.display = (currentStyle === 'multisample') ? 'none' : '';
    });
  });
  // Initial state — if multisample is the active style on load, hide compose
  setTimeout(() => {
    const composeCard = document.getElementById('compose-card');
    if (composeCard && currentStyle === 'multisample') composeCard.style.display = 'none';
  }, 100);

  // ---------- LIVE waveform — always-on streaming oscilloscope of the input ----------
  // Uses the browser's Web Audio API to read from TX-6 (or whatever input device)
  // in real time, draws Serato-style waveform on a canvas continuously. Play the
  // synth → you see the waveform breathe. Silent = flat line. No clicks needed
  // after initial connect (Safari requires one-time user gesture for getUserMedia).
  let liveWaveStream = null;
  let liveWaveCtx = null;
  let liveWaveAnimFrame = null;
  let liveWavePeakHold = 0;
  let liveWavePeakHoldT = 0;
  // Y-axis zoom: peaks scale to this fraction of half-canvas height
  let liveWaveHeadroom = 0.75;
  // TIME-axis zoom (seconds visible across canvas width):
  //   0.043 (≈ FFT 2048 @ 48k) = instant scope, sees individual oscillation cycles
  //   1, 3, 7, 15 = scrolling peak history view, sees attack/decay envelope
  // Switches modes automatically at 0.7s threshold.
  const TIME_LEVELS = [0.043, 0.25, 1, 3, 7, 15, 30];
  let liveWaveTimeIdx = 0;  // index into TIME_LEVELS
  // Peak history buffer for scrolling-history mode
  let livePeakHistory = [];   // {t, peakP, peakN}
  let livePeakHistoryStartT = 0;
  // Spectrogram scroll timing
  let liveWaveLastFrameMs = 0;

  async function startLiveWave() {
    const settings = getAudioSettings();
    const deviceName = settings.audio_device || 'TX-6';
    const channelStr = settings.input_channels || '1,2';
    const desiredChannels = channelStr.split(',').map(c => parseInt(c.trim()) - 1);  // 0-indexed

    // Find the audio input device by name in the browser's media devices
    try {
      // Ask for permission first (required to get full device labels)
      await navigator.mediaDevices.getUserMedia({ audio: true });
      const devs = await navigator.mediaDevices.enumerateDevices();
      const audioInputs = devs.filter(d => d.kind === 'audioinput');
      const match = audioInputs.find(d => d.label.toLowerCase().includes(deviceName.toLowerCase()));
      if (!match) {
        document.getElementById('ms-live-wave-label').textContent = `✗ no audio device matches "${deviceName}" in browser. found: ${audioInputs.map(d => d.label).slice(0,3).join(', ')}`;
        return;
      }
      // Request the actual stream from that device
      liveWaveStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          deviceId: { exact: match.deviceId },
          echoCancellation: false, noiseSuppression: false, autoGainControl: false,
          sampleRate: 48000, channelCount: Math.max(...desiredChannels) + 1 || 2,
        },
      });

      liveWaveCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000 });
      const source = liveWaveCtx.createMediaStreamSource(liveWaveStream);
      const splitter = liveWaveCtx.createChannelSplitter(source.channelCount || 2);
      source.connect(splitter);

      // Mix selected channels to mono
      const merger = liveWaveCtx.createChannelMerger(1);
      const gain = liveWaveCtx.createGain();
      gain.gain.value = 1 / Math.max(1, desiredChannels.length);
      desiredChannels.forEach(ch => {
        if (ch < (source.channelCount || 2)) splitter.connect(gain, ch);
      });
      gain.connect(merger, 0, 0);

      // PSYCHEDELIC 3-BAND SPLIT — low/mid/high each get their own analyser + color.
      // Filters: low <250Hz, mid 250-4000Hz, high >4000Hz. Roland-style spectrum.
      function makeAnalyser(filterChain) {
        const an = liveWaveCtx.createAnalyser();
        an.fftSize = 2048; an.smoothingTimeConstant = 0.0;
        filterChain.connect(an);
        return an;
      }
      // Low band: lowpass 250Hz
      const lpLow = liveWaveCtx.createBiquadFilter();
      lpLow.type = 'lowpass'; lpLow.frequency.value = 250; lpLow.Q.value = 0.7;
      merger.connect(lpLow);
      const anLow = makeAnalyser(lpLow);
      // Mid band: bandpass 250-4000Hz (highpass then lowpass)
      const hpMid = liveWaveCtx.createBiquadFilter();
      hpMid.type = 'highpass'; hpMid.frequency.value = 250; hpMid.Q.value = 0.7;
      const lpMid = liveWaveCtx.createBiquadFilter();
      lpMid.type = 'lowpass'; lpMid.frequency.value = 4000; lpMid.Q.value = 0.7;
      merger.connect(hpMid); hpMid.connect(lpMid);
      const anMid = makeAnalyser(lpMid);
      // High band: highpass 4000Hz
      const hpHigh = liveWaveCtx.createBiquadFilter();
      hpHigh.type = 'highpass'; hpHigh.frequency.value = 4000; hpHigh.Q.value = 0.7;
      merger.connect(hpHigh);
      const anHigh = makeAnalyser(hpHigh);
      // Full-band analyser — used for peak metering AND for spectrogram frequency data.
      // fftSize 8192 → 4096 freq bins at 48kHz → ~6Hz resolution (musical detail intact).
      const anFull = liveWaveCtx.createAnalyser();
      anFull.fftSize = 8192;
      anFull.smoothingTimeConstant = 0.5;  // a touch of smoothing makes the spectrogram less twitchy
      anFull.minDecibels = -90;
      anFull.maxDecibels = -10;
      merger.connect(anFull);

      const dLow = new Float32Array(anLow.fftSize);
      const dMid = new Float32Array(anMid.fftSize);
      const dHigh = new Float32Array(anHigh.fftSize);
      const dFull = new Float32Array(anFull.fftSize);
      // Frequency-domain data for spectrogram (history mode). Byte form is faster + sufficient.
      const dFreq = new Uint8Array(anFull.frequencyBinCount);  // 4096 bins at fftSize 8192

      const canvas = document.getElementById('ms-live-wave-canvas');
      const cctx = canvas.getContext('2d');
      const W = canvas.width, H = canvas.height;
      // Y-axis zoom is now in module state (liveWaveHeadroom) — controlled by + / − buttons

      // OPTIMIZED — build one Path2D per band, fill once. No per-pixel shadowBlur.
      // Cuts draw time from ~15ms to ~1ms per frame.
      function drawBand(data, color, alpha) {
        cctx.globalCompositeOperation = 'screen';
        cctx.globalAlpha = alpha;
        cctx.fillStyle = color;
        const samplesPerPx = Math.max(1, Math.floor(data.length / W));
        const path = new Path2D();
        const reflPath = new Path2D();
        for (let x = 0; x < W; x++) {
          let max = 0, min = 0;
          const start = x * samplesPerPx;
          const end = Math.min(data.length, start + samplesPerPx);
          for (let i = start; i < end; i++) {
            if (data[i] > max) max = data[i];
            if (data[i] < min) min = data[i];
          }
          const yMax = H/2 - max * (H/2) * liveWaveHeadroom;
          const yMin = H/2 - min * (H/2) * liveWaveHeadroom;
          path.rect(x, yMax, 1, Math.max(1, yMin - yMax));
          // Mirror — inverted, will be filled with reduced alpha
          const reflTop = H/2;
          const reflBot = H/2 + (max - min) * (H/2) * liveWaveHeadroom * 0.5;
          reflPath.rect(x, reflTop, 1, Math.max(1, reflBot - reflTop));
        }
        // One fill for the wave
        cctx.fill(path);
        // One fill for the mirror at 30%
        cctx.globalAlpha = alpha * 0.3;
        cctx.fill(reflPath);
      }

      function draw() {
        const timeWindowSec = TIME_LEVELS[liveWaveTimeIdx];
        const now = performance.now();

        anFull.getFloatTimeDomainData(dFull);
        // Compute full-band peaks (positive + negative for proper history)
        let peakP = 0, peakN = 0, peakAbs = 0;
        for (let i = 0; i < dFull.length; i++) {
          if (dFull[i] > peakP) peakP = dFull[i];
          if (dFull[i] < peakN) peakN = dFull[i];
          const a = Math.abs(dFull[i]);
          if (a > peakAbs) peakAbs = a;
        }
        if (peakAbs > liveWavePeakHold || now - liveWavePeakHoldT > 1500) {
          liveWavePeakHold = peakAbs; liveWavePeakHoldT = now;
        } else {
          liveWavePeakHold = Math.max(peakAbs, liveWavePeakHold * 0.97);
        }
        const peakDb = liveWavePeakHold > 0 ? 20 * Math.log10(liveWavePeakHold) : null;

        // Push to peak-history buffer (for history mode)
        livePeakHistory.push({ t: now, p: peakP, n: peakN });
        // Prune old entries beyond the longest possible time window (30s)
        const maxHistMs = TIME_LEVELS[TIME_LEVELS.length - 1] * 1000 + 500;
        while (livePeakHistory.length && now - livePeakHistory[0].t > maxHistMs) {
          livePeakHistory.shift();
        }

        // Color picks — danger pivot if clipping
        let colorLow, colorMid, colorHigh, colorMain;
        if (liveWavePeakHold > 0.92) {
          colorLow = '#ff2a2a'; colorMid = '#ff7700'; colorHigh = '#ffdd00'; colorMain = '#ff5b1f';
        } else if (liveWavePeakHold < 0.005) {
          colorLow = '#2a2a2a'; colorMid = '#3a3a3a'; colorHigh = '#4a4a4a'; colorMain = '#3a3a3a';
        } else {
          colorLow = '#ff2dbe'; colorMid = '#00e8e8'; colorHigh = '#ffd633'; colorMain = '#F2B705';
        }

        if (timeWindowSec <= 0.5) {
          // ===== INSTANT FFT MODE (3-band psychedelic scope) =====
          anLow.getFloatTimeDomainData(dLow);
          anMid.getFloatTimeDomainData(dMid);
          anHigh.getFloatTimeDomainData(dHigh);

          // TRAIL (only in instant mode — history doesn't need it)
          cctx.globalCompositeOperation = 'source-over';
          cctx.globalAlpha = 1;
          cctx.fillStyle = 'rgba(5, 5, 8, 0.35)';
          cctx.fillRect(0, 0, W, H);

          // Center line
          cctx.strokeStyle = 'rgba(60, 40, 80, 0.4)';
          cctx.lineWidth = 1;
          cctx.beginPath(); cctx.moveTo(0, H/2); cctx.lineTo(W, H/2); cctx.stroke();

          drawBand(dLow, colorLow, 0.75);
          drawBand(dMid, colorMid, 0.85);
          drawBand(dHigh, colorHigh, 0.65);
        } else {
          // ===== SPECTROGRAM WATERFALL MODE =====
          // Scrolling frequency-vs-time. Y = log frequency (sub at bottom, brilliance at top).
          // Color = magnitude. Psychedelic palette: magenta sub → cyan mid → yellow high.
          // Each frame: shift canvas left by N pixels, paint new column at right edge.
          anFull.getByteFrequencyData(dFreq);

          // How many pixels to shift per frame based on time window:
          //   short window (e.g. 1s) = wave moves fast (4-8 px/frame)
          //   long window (e.g. 30s) = wave moves slow (1 px/frame)
          const targetMsPerPx = (timeWindowSec * 1000) / W;
          const msSinceLast = liveWaveLastFrameMs ? (now - liveWaveLastFrameMs) : 16.67;
          liveWaveLastFrameMs = now;
          const shiftPx = Math.max(1, Math.round(msSinceLast / targetMsPerPx));

          // Shift existing pixels left
          cctx.globalCompositeOperation = 'source-over';
          cctx.globalAlpha = 1;
          const imgData = cctx.getImageData(shiftPx, 0, W - shiftPx, H);
          cctx.putImageData(imgData, 0, 0);
          // Clear the new column area
          cctx.fillStyle = '#050505';
          cctx.fillRect(W - shiftPx, 0, shiftPx, H);

          // Paint new column(s) at right edge. Log-frequency Y axis so each octave is even.
          // 20Hz at bottom, 20kHz at top. Frequency bin → frequency: i × (sampleRate / fftSize).
          const sr = liveWaveCtx.sampleRate;
          const binToFreq = i => (i * sr) / anFull.fftSize;
          const minF = 30, maxF = 18000;
          const logMin = Math.log2(minF), logMax = Math.log2(maxF);

          for (let y = 0; y < H; y++) {
            // y=0 is top of canvas = highest frequency. Invert for "low at bottom."
            const freqLog = logMax - (y / H) * (logMax - logMin);
            const freq = Math.pow(2, freqLog);
            const bin = Math.round((freq * anFull.fftSize) / sr);
            if (bin < 0 || bin >= dFreq.length) continue;
            const mag = dFreq[bin] / 255;  // 0-1
            if (mag < 0.05) continue;  // skip near-silence pixels

            // Color by frequency band (psychedelic)
            let r, g, b;
            if (freq < 250) {
              // sub/low: magenta
              r = 255; g = 45 + Math.floor(mag * 80); b = 190;
            } else if (freq < 2000) {
              // mid: cyan to teal
              r = 0; g = 230; b = 232;
            } else if (freq < 6000) {
              // hi-mid: green
              r = 110; g = 240; b = 110;
            } else {
              // high/air: yellow → white
              r = 255; g = 214 + Math.floor(mag * 40); b = 51 + Math.floor(mag * 100);
            }
            // Magnitude → alpha
            const alpha = Math.min(1, mag * 1.5);
            // Clipping = red overlay
            if (liveWavePeakHold > 0.92) { r = 255; g = 80; b = 80; }
            cctx.fillStyle = `rgba(${r},${g},${b},${alpha})`;
            cctx.fillRect(W - shiftPx, y, shiftPx, 1);
          }

          // Time-window label in bottom-left
          cctx.fillStyle = 'rgba(255, 215, 5, 0.7)';
          cctx.font = '10px monospace';
          cctx.fillText(`${timeWindowSec.toFixed(timeWindowSec < 1 ? 2 : 0)}s · spectrogram (low ↓ high ↑)`, 6, H - 6);
          // Octave grid markers on left edge (subtle)
          cctx.fillStyle = 'rgba(160, 100, 200, 0.3)';
          cctx.font = '8px monospace';
          for (const label of [{f: 100, t: '100'}, {f: 1000, t: '1k'}, {f: 10000, t: '10k'}]) {
            const yMark = H - ((Math.log2(label.f) - logMin) / (logMax - logMin)) * H;
            cctx.fillText(label.t, 2, yMark);
          }
        }

        cctx.shadowBlur = 0;
        cctx.globalCompositeOperation = 'source-over';
        cctx.globalAlpha = 1;

        // Peak readout
        const peakEl = document.getElementById('ms-live-wave-peak');
        if (peakEl) {
          if (peakDb === null || liveWavePeakHold < 0.0001) {
            peakEl.textContent = '— SILENT —';
            peakEl.style.color = '#d65656';
          } else {
            peakEl.textContent = `${peakDb.toFixed(1)} dBFS`;
            peakEl.style.color = peakDb > -3 ? '#ff2a2a' : peakDb < -30 ? '#806550' : '#00e8e8';
          }
        }
        liveWaveAnimFrame = requestAnimationFrame(draw);
      }

      // Update label + buttons
      document.getElementById('ms-live-wave-label').textContent = `🎚 LIVE · ${match.label} · ch ${channelStr}`;
      document.getElementById('ms-live-wave-connect-btn').style.display = 'none';
      document.getElementById('ms-live-wave-stop-btn').style.display = 'inline-block';
      draw();
    } catch (e) {
      document.getElementById('ms-live-wave-label').textContent = `✗ live wave error: ${e.message}`;
      console.error('live wave error', e);
    }
  }

  function stopLiveWave() {
    if (liveWaveAnimFrame) { cancelAnimationFrame(liveWaveAnimFrame); liveWaveAnimFrame = null; }
    if (liveWaveStream) {
      liveWaveStream.getTracks().forEach(t => t.stop());
      liveWaveStream = null;
    }
    if (liveWaveCtx) { try { liveWaveCtx.close(); } catch (e) {} liveWaveCtx = null; }
    document.getElementById('ms-live-wave-connect-btn').style.display = 'inline-block';
    document.getElementById('ms-live-wave-stop-btn').style.display = 'none';
    document.getElementById('ms-live-wave-label').textContent = '🎚 LIVE INPUT — click "Connect" to start streaming';
    document.getElementById('ms-live-wave-peak').textContent = '— dBFS';
    // Clear canvas
    const canvas = document.getElementById('ms-live-wave-canvas');
    if (canvas) {
      const cctx = canvas.getContext('2d');
      cctx.fillStyle = '#050505';
      cctx.fillRect(0, 0, canvas.width, canvas.height);
    }
  }

  document.getElementById('ms-live-wave-connect-btn')?.addEventListener('click', startLiveWave);
  document.getElementById('ms-live-wave-stop-btn')?.addEventListener('click', stopLiveWave);

  // AMP zoom — Y axis (amplitude). 25% to 150%.
  document.getElementById('ms-live-wave-zoom-out')?.addEventListener('click', () => {
    liveWaveHeadroom = Math.max(0.25, liveWaveHeadroom - 0.15);
    setStatus(`amp zoom: ${Math.round(liveWaveHeadroom * 100)}%`, '');
  });
  document.getElementById('ms-live-wave-zoom-in')?.addEventListener('click', () => {
    liveWaveHeadroom = Math.min(1.5, liveWaveHeadroom + 0.15);
    setStatus(`amp zoom: ${Math.round(liveWaveHeadroom * 100)}%`, '');
  });
  // TIME zoom — X axis. Steps through TIME_LEVELS = [0.043s (instant), 0.25s, 1s, 3s, 7s, 15s, 30s].
  // Above 0.5s switches to scrolling peak-history mode — you SEE the full envelope of a sustained note.
  document.getElementById('ms-live-wave-time-out')?.addEventListener('click', () => {
    liveWaveTimeIdx = Math.min(TIME_LEVELS.length - 1, liveWaveTimeIdx + 1);
    setStatus(`time window: ${TIME_LEVELS[liveWaveTimeIdx]}s ${liveWaveTimeIdx === 0 ? '(instant scope)' : '(envelope history)'}`, '');
  });
  document.getElementById('ms-live-wave-time-in')?.addEventListener('click', () => {
    liveWaveTimeIdx = Math.max(0, liveWaveTimeIdx - 1);
    setStatus(`time window: ${TIME_LEVELS[liveWaveTimeIdx]}s ${liveWaveTimeIdx === 0 ? '(instant scope)' : '(envelope history)'}`, '');
  });

  // ---------- (old) multisample audio meter — pre-flight check before captures ----------
  document.getElementById('ms-meter-btn')?.addEventListener('click', async () => {
    const settings = getAudioSettings();
    const btn = document.getElementById('ms-meter-btn');
    const readout = document.getElementById('ms-meter-readout');
    const bars = document.getElementById('ms-meter-bars');
    btn.disabled = true;
    btn.textContent = '🔴 Recording 3 sec — PLAY THE SYNTH NOW';
    readout.style.color = 'var(--amber)';
    readout.textContent = `recording on ${settings.audio_device} ch ${settings.input_channels} for 3 seconds — play a sustained chord on the synth NOW…`;
    bars.style.display = 'none';
    try {
      const r = await fetch('/api/meter', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ duration: 3, ...settings }),
      });
      const data = await r.json();
      btn.disabled = false;
      btn.textContent = '▶ Check 3 sec (play synth now)';
      if (!data.ok) {
        readout.style.color = 'var(--red)';
        readout.textContent = `error: ${data.error || 'unknown'}`;
        return;
      }
      const db = data.peak_db;
      // Convert dBFS to a visual percentage (0 dBFS = 100%, -60 dBFS = 0%)
      const pct = db === null ? 0 : Math.max(0, Math.min(100, 100 + (db / 60) * 100));
      let color, verdict;
      if (db === null || db < -40) {
        color = 'var(--red)';
        verdict = `✗ SILENT (${db === null ? 'no signal' : db.toFixed(1) + ' dBFS'}) — captures will be empty. Check TX-6 channels, synth volume, cables. NOT safe to capture.`;
      } else if (db > -3) {
        color = 'var(--amber)';
        verdict = `⚠ HOT (peak ${db.toFixed(1)} dBFS) — lower input gain on the TX-6 to prevent clipping`;
      } else if (db < -18) {
        color = 'var(--amber)';
        verdict = `↑ QUIET (peak ${db.toFixed(1)} dBFS) — captures will work but raise input gain for more headroom`;
      } else {
        color = 'var(--green)';
        verdict = `✓ HEALTHY peak ${db.toFixed(1)} dBFS on ${settings.audio_device} ch ${settings.input_channels} — safe to capture`;
      }
      readout.style.color = color;
      readout.textContent = verdict;
      // Visual bar
      bars.style.display = 'block';
      bars.innerHTML = `
        <div style="height: 24px; background: var(--bg-2); border: 1px solid var(--border); border-radius: 2px; overflow: hidden; position: relative;">
          <div style="height: 100%; width: ${pct}%; background: ${color}; transition: width 0.3s;"></div>
          <div style="position: absolute; top: 0; left: 0; right: 0; bottom: 0; display: flex; align-items: center; justify-content: center; font-family: var(--mono); font-size: 11px; font-weight: 700; color: var(--fg); text-shadow: 0 0 4px rgba(0,0,0,0.7);">${db === null ? '— dBFS' : db.toFixed(1) + ' dBFS'}</div>
        </div>
        <div style="margin-top: 4px; font-size: 9px; color: var(--fg-faint); display: flex; justify-content: space-between;">
          <span>-60</span><span>-40 silent ↑</span><span>-18 healthy ↑</span><span>-3 clip ↑</span><span>0</span>
        </div>
      `;
    } catch (e) {
      btn.disabled = false;
      btn.textContent = '▶ Check 3 sec (play synth now)';
      readout.style.color = 'var(--red)';
      readout.textContent = `error: ${e.message}`;
    }
  });

  els.instrumentSelect.addEventListener('change', () => {
    const slug = els.instrumentSelect.value;
    // Remember this choice across refreshes
    try { localStorage.setItem('bench.lastInstrument', slug); } catch (e) {}
    loadInstrument(slug);
    loadInstrumentSettings(slug);
    loadIconicPatches(slug);
  });

  // BANG IT OUT — auto-capture queue. Hit one button, walks through every iconic
  // patch in sequence, fires PC + multisamples each. Default skips already-captured.
  let queuePollTimer = null;

  function renderQueueProgress(s) {
    const el = document.getElementById('queue-progress');
    if (!el) return;
    if (!s.running && !s.finished_at) { el.style.display = 'none'; return; }
    el.style.display = 'block';
    const done = s.completed.length;
    const failed = s.failed.length;
    const skipped = s.skipped.length;
    if (s.running) {
      const pct = s.total > 0 ? Math.round((done / s.total) * 100) : 0;
      el.innerHTML = `
        <div style="color: var(--amber); font-weight: 600; margin-bottom: 6px;">[${s.current_idx}/${s.total}] ${s.current_patch || '...'}</div>
        <div style="height: 4px; background: var(--bg-2); margin-bottom: 8px; border-radius: 2px; overflow: hidden;">
          <div style="height: 100%; width: ${pct}%; background: var(--grad-amber); transition: width 0.3s;"></div>
        </div>
        <div style="color: var(--fg-dim); font-size: 10px;">${done} captured · ${failed} failed · ${skipped} skipped</div>
        ${s.log.slice(-3).map(line => `<div style="color: var(--fg-faint); font-size: 10px; margin-top: 4px; font-family: var(--mono);">${line}</div>`).join('')}
      `;
    } else if (s.finished_at) {
      const wavTotal = s.completed.reduce((acc, c) => acc + (c.wavs || 0), 0);
      const elapsed = s.completed.reduce((acc, c) => acc + (c.elapsed_sec || 0), 0);
      el.innerHTML = `
        <div style="color: var(--green); font-weight: 700; font-size: 14px; margin-bottom: 6px;">✓ DONE</div>
        <div style="color: var(--fg); font-size: 11px;">${done} patches · ${wavTotal} samples · ${Math.round(elapsed)}s</div>
        ${failed > 0 ? `<div style="color: var(--red); font-size: 10px; margin-top: 4px;">${failed} failed: ${s.failed.map(f => f.patch).join(', ')}</div>` : ''}
      `;
    }
  }

  async function pollQueueStatus() {
    try {
      const r = await fetch('/api/capture-queue/status');
      const s = await r.json();
      renderQueueProgress(s);
      if (!s.running) {
        // Queue ended — stop polling + flip UI back to "start" state
        if (queuePollTimer) { clearInterval(queuePollTimer); queuePollTimer = null; }
        document.getElementById('bang-it-out-btn').style.display = 'block';
        document.getElementById('bang-it-out-stop-btn').style.display = 'none';
        // Refresh the iconic patches list so newly-captured rows show ✓
        const slug = document.getElementById('instrument-select')?.value;
        if (slug) loadIconicPatches(slug);
      }
    } catch (e) { /* swallow poll errors */ }
  }

  document.getElementById('bang-it-out-btn')?.addEventListener('click', async () => {
    const slug = document.getElementById('instrument-select')?.value;
    if (!slug) { setStatus('No instrument selected', 'error'); return; }
    const settings = getAudioSettings();
    if (!settings.midi_port || !settings.audio_device) {
      setStatus('Set audio routing first (⚙ Audio routing panel)', 'error');
      return;
    }
    if (!confirm(`Auto-capture all iconic patches for ${slug}?\n\nMode: todo (skip already-captured)\nThis will take a few minutes — walk away when it starts.`)) return;
    document.getElementById('bang-it-out-btn').style.display = 'none';
    document.getElementById('bang-it-out-stop-btn').style.display = 'block';
    try {
      const r = await fetch('/api/capture-queue/start', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          instrument: slug,
          mode: 'todo',
          chain: 'raw',
          settings: {
            midi_port: settings.midi_port,
            midi_channel: 1,
            audio_device: settings.audio_device,
            input_channels: settings.input_channels || '1,2',
            note_range: (document.getElementById('ms-range')?.value.trim() || 'C2-C5'),
            step: 3,
            velocities: '100',
            round_robins: 1,
            sustain_sec: 4,
            tail_sec: 2.5,
          },
        }),
      });
      const d = await r.json();
      if (d.error) {
        setStatus(`Queue failed: ${d.error}`, 'error');
        document.getElementById('bang-it-out-btn').style.display = 'block';
        document.getElementById('bang-it-out-stop-btn').style.display = 'none';
        return;
      }
      setStatus(`🎯 Queue started — ${d.total} patches to capture (${d.skipped?.length || 0} skipped)`, 'success');
      pollQueueStatus();
      queuePollTimer = setInterval(pollQueueStatus, 1500);
    } catch (e) {
      setStatus(`Queue error: ${e.message}`, 'error');
      document.getElementById('bang-it-out-btn').style.display = 'block';
      document.getElementById('bang-it-out-stop-btn').style.display = 'none';
    }
  });

  document.getElementById('bang-it-out-stop-btn')?.addEventListener('click', async () => {
    if (!confirm('Stop the auto-capture queue?')) return;
    await fetch('/api/capture-queue/stop', { method: 'POST' });
    setStatus('Queue stopping (finishing current patch first)...', '');
  });

  // Populate + reveal the "now selected patch" detail card in the main panel.
  // Called when a user clicks an iconic-patch row in the sidebar — pulls up the
  // patch's name, vibe, and the two big buttons (reload-on-synth + capture).
  function showPatchDetail(patchData, presetId) {
    const card = document.getElementById('patch-detail-card');
    if (!card) return;
    // Show the REAL preset name (e.g. "A33 · JP-303"), not the internal slug ("tb-echo").
    // Applies to every patch via preset_name from the manifest.
    const realName = patchData.preset_name
      ? `${patchData.preset_position ? patchData.preset_position + ' · ' : ''}${patchData.preset_name}`
      : patchData.name;
    document.getElementById('patch-detail-name').textContent = realName;
    const instrSlug = document.getElementById('instrument-select')?.value || '';
    const instrName = document.getElementById('instrument-select')?.selectedOptions[0]?.textContent || instrSlug;
    document.getElementById('patch-detail-instrument').textContent = instrName;
    document.getElementById('patch-detail-vibe').textContent = patchData.notes || '(no notes scaffolded for this patch yet)';
    const pcPill = document.getElementById('patch-detail-pc-pill');
    if (patchData.program_change != null) {
      pcPill.textContent = presetId || `PC ${patchData.program_change}`;
      pcPill.style.display = 'block';
    } else {
      pcPill.style.display = 'none';
    }
    const statusEl = document.getElementById('patch-detail-status');
    if (patchData.status === 'captured') {
      statusEl.innerHTML = `<span style="color: var(--green);">✓ ${patchData.wav_count} samples captured</span>`;
    } else if (patchData.status === 'pending') {
      statusEl.innerHTML = `<span style="color: var(--amber);">◐ partial capture</span>`;
    } else {
      statusEl.textContent = '○ not captured yet';
    }
    // Wire the two buttons — re-bind every reveal so they target the current patch
    const fireBtn = document.getElementById('patch-fire-pc-btn');
    const capBtn = document.getElementById('patch-capture-btn');
    fireBtn.style.display = (patchData.program_change != null) ? 'inline-block' : 'none';
    fireBtn.onclick = async () => {
      const settings = getAudioSettings();
      const midiPort = settings.midi_port;
      if (!midiPort) { setStatus('No MIDI port selected', 'error'); return; }
      fireBtn.disabled = true; fireBtn.textContent = '⏳ Sending…';
      try {
        const r = await fetch('/api/load-patch', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ midi_port: midiPort, channel: 1, program_change: parseInt(patchData.program_change, 10), patch_name: patchData.name }),
        });
        const d = await r.json();
        setStatus(d.ok ? `🎛 ${patchData.name} → ${d.midi_port}` : `Load failed: ${d.error}`, d.ok ? 'success' : 'error');
      } finally {
        fireBtn.disabled = false; fireBtn.textContent = '🎛 Reload on synth';
      }
    };
    capBtn.onclick = () => {
      // Jump down to the capture form (already pre-filled by the row click)
      const msBtn = document.querySelector('.styles button[data-style="multisample"]');
      if (msBtn) msBtn.click();
      document.querySelector('section.card h3')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      setStatus(`📸 Multisample form pre-filled with "${patchData.name}" — hit Capture when ready`, '');
    };

    // BIND FROM SYNTH — start listening; when a PC comes in, capture + offer to bind
    const bindBtn = document.getElementById('patch-bind-btn');
    bindBtn.onclick = () => startBindFlow(patchData);

    // Render the captured-multisamples strip if patch has WAVs
    renderPatchWavStrip(patchData);

    // Reveal + scroll into view
    card.style.display = 'block';
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // STRIP VIEW — all N captured multisample WAVs as mini Serato waveforms in a row.
  // Visual proof of capture health: silent ones show as flat gray lines, healthy bloom amber.
  // Zoom +/- changes tile width.
  let wavstripTileWidth = 180;  // px per waveform tile — big enough for 7-sec sustains to breathe
  let wavstripTileHeight = 110;  // taller for amplitude detail
  const WAVSTRIP_MIN = 40, WAVSTRIP_MAX = 360;
  let wavstripPatch = null;     // patch name currently shown in the strip (for the wipe button)
  let captureTarget = null;     // factory preset just clicked in the catalog — its slot link rides along on the next multisample capture so the slot flips ✓ + click-to-load works even when renamed
  let currentPreset = null;     // the sound now loaded on the synth (last preset click) — stamped onto any progression you fire, so a take always knows its patch
  let capturedPatches = {};     // name → wav_count for patches that already have captures — the overwrite guard checks this so a stale name can't silently clobber an existing patch
  try { const _cp = localStorage.getItem('benchCurrentPreset'); if (_cp) currentPreset = JSON.parse(_cp); } catch (e) {}
  function updatePresetReadout() {
    const el = document.getElementById('current-preset-readout');
    if (!el) return;
    el.innerHTML = currentPreset
      ? `🎛 sound: <b style="color: var(--fg);">${currentPreset.display}</b> <span style="color: var(--fg-faint);">— fired progressions get stamped with this</span>`
      : `🎛 sound: <span style="color: var(--fg-faint);">— click a preset to load one</span>`;
  }

  async function renderPatchWavStrip(patchData) {
    const block = document.getElementById('patch-wavstrip-block');
    if (!block) return;
    if (patchData.status === 'todo' || patchData.wav_count === 0) {
      block.style.display = 'none';
      return;
    }
    const slug = document.getElementById('instrument-select')?.value;
    if (!slug) { block.style.display = 'none'; return; }
    // Try each chain that has wavs
    const chain = patchData.chains_with_wavs?.[0] || 'raw';
    let data;
    try {
      const r = await fetch(`/api/instruments/${slug}/patches/${encodeURIComponent(patchData.name)}/${chain}/wavs`);
      data = await r.json();
    } catch (e) {
      block.style.display = 'none';
      return;
    }
    if (!data.wavs || data.wavs.length === 0) {
      block.style.display = 'none';
      return;
    }
    block.style.display = 'block';
    const _rn = patchData.preset_name || patchData.name;
    document.getElementById('patch-wavstrip-title').textContent = `Captured multisamples · ${_rn} / ${chain}`;
    document.getElementById('patch-wavstrip-count').textContent = `${data.wavs.length} samples`;

    const strip = document.getElementById('patch-wavstrip');
    strip.innerHTML = data.wavs.map(w => `
      <div class="wav-tile" data-wav-url="${w.url}" style="flex-shrink: 0; width: ${wavstripTileWidth}px; height: 60px; background: #0a0a0a; border: 1px solid var(--border); border-radius: 2px; position: relative; cursor: pointer;" title="${w.name} · ${w.size_kb} KB">
        <canvas class="wav-tile-canvas" width="${wavstripTileWidth * 2}" height="120" style="width: 100%; height: 100%; display: block;"></canvas>
        <div style="position: absolute; bottom: 2px; left: 4px; right: 4px; font-family: var(--mono); font-size: 9px; color: var(--fg-faint); text-shadow: 0 0 3px #000; text-align: center; pointer-events: none;">${w.note || ''}</div>
      </div>
    `).join('');

    // Decode + draw each tile. Click a tile → open the LOOP EDITOR for that note
    // (set the loop by ear, snap to zero crossings, audition, lock).
    strip.querySelectorAll('.wav-tile').forEach((tile, idx) => {
      const url = tile.dataset.wavUrl;
      const canvas = tile.querySelector('.wav-tile-canvas');
      drawWavTile(canvas, url);
      tile.addEventListener('click', () => openLoopEditor(slug, patchData.name, chain, data.wavs[idx]));
    });
  }

  // ===================== LOOP EDITOR =====================
  // The ear-in-the-loop fix. You see the waveform, drag green(start)/red(end) handles that
  // snap to zero crossings, and hear the EXACT loop live (Web Audio loop = sample-accurate,
  // identical to Ableton's Sampler). Lock saves the points; the pack bakes exactly them.
  const LE = { buffer: null, ch0: null, sr: 0, zc: null, S: 0, E: 0,
               slug: '', patch: '', chain: 'raw', filename: '', note: '',
               source: null, playing: false, dragging: null, history: [],
               view: { start: 0, end: 0 },   // visible sample window (time zoom)
               amp: 1 };                      // amplitude (vertical) zoom — blow up quiet detail

  function lePushHistory() {                  // call before a change so it can be undone
    LE.history.push({ S: LE.S, E: LE.E });
    if (LE.history.length > 100) LE.history.shift();
  }
  function leUndo() {
    if (!LE.history.length) { document.getElementById('le-status').textContent = 'nothing to undo'; return; }
    const prev = LE.history.pop();
    LE.S = prev.S; LE.E = prev.E;
    drawLoopEditor();
    if (LE.source) { LE.source.loopStart = LE.S / LE.sr; LE.source.loopEnd = LE.E / LE.sr; }
  }

  function leZeroCrossings(d) {
    const zc = [];
    for (let i = 1; i < d.length; i++) if (d[i - 1] <= 0 && d[i] > 0) zc.push(i);
    return Int32Array.from(zc);
  }
  function leSnap(idx) {
    if (!document.getElementById('le-snap').checked || !LE.zc || !LE.zc.length) return idx;
    let lo = 0, hi = LE.zc.length - 1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (LE.zc[m] < idx) lo = m + 1; else hi = m - 1; }
    const a = LE.zc[Math.max(0, hi)], b = LE.zc[Math.min(LE.zc.length - 1, lo)];
    return Math.abs(a - idx) <= Math.abs(b - idx) ? a : b;
  }

  async function openLoopEditor(slug, patch, chain, wav) {
    if (!wav) return;
    const panel = document.getElementById('loop-editor');
    const wasOpen = panel.style.display === 'block';
    const wasPlaying = LE.playing;          // carry the audition across note switches
    leStop();
    LE.history = [];                        // fresh undo history per note
    LE.view = { start: 0, end: 0 };         // reset zoom to full (drawLoopEditor fills it in)
    LE.slug = slug; LE.patch = patch; LE.chain = chain; LE.filename = wav.name; LE.note = wav.note || '';
    document.getElementById('le-note').textContent = wav.note ? wav.note.toUpperCase() : wav.name;
    document.getElementById('le-status').textContent = 'decoding…';
    panel.style.display = 'block';
    if (!wasOpen) panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });  // only scroll on first open
    // highlight the active tile in the strip
    document.querySelectorAll('.wav-tile').forEach(t => t.style.outline = '');
    const activeTile = [...document.querySelectorAll('.wav-tile')].find(t => t.dataset.wavUrl === wav.url);
    if (activeTile) activeTile.style.outline = '2px solid var(--amber)';
    try {
      const ac = ensureAudioContext();
      const ab = await (await fetch(wav.url)).arrayBuffer();
      LE.buffer = await ac.decodeAudioData(ab);
      LE.sr = LE.buffer.sampleRate;
      LE.ch0 = LE.buffer.getChannelData(0);
      LE.zc = leZeroCrossings(LE.ch0);
      const n = LE.ch0.length;
      // Show the REAL loop that would ship: locked (manual) if set, else the build's auto-detect.
      // So an untouched note already displays what ships — no need to lock unless you change it.
      let loop = (wav.loop && wav.loop.end > wav.loop.start) ? wav.loop : null;
      let src = loop ? 'locked' : 'default';
      if (!loop) {
        try {
          const r = await fetch(`/api/patch/auto-loop?instrument=${encodeURIComponent(slug)}&patch=${encodeURIComponent(patch)}&chain=${encodeURIComponent(chain)}&filename=${encodeURIComponent(wav.name)}`);
          const d = await r.json();
          if (d.loop && d.loop.end > d.loop.start) { loop = d.loop; src = 'auto'; }
        } catch (e) {}
      }
      if (loop) { LE.S = loop.start; LE.E = loop.end; }
      else { LE.S = leSnap(Math.floor(0.40 * n)); LE.E = leSnap(Math.floor(0.40 * n) + Math.floor(0.4 * LE.sr)); }
      // ALWAYS hard loop (crossfade 0) — that's exactly what the audition plays, and what the
      // build now bakes for manual loops. A crossfade here is what made "perfect in editor,
      // bad in build": the editor hard-loops, the build was adding a crossfade that warbled.
      const xf0 = 0;
      document.getElementById('le-xfade').value = xf0;
      document.getElementById('le-xfade-val').textContent = xf0;
      drawLoopEditor();
      const srcMsg = src === 'locked' ? 'showing your LOCKED loop' :
                     src === 'auto'   ? "showing the AUTO loop — this is what ships if you don't change it" :
                                        'showing a default — drag + lock to set';
      document.getElementById('le-status').textContent =
        `${(n / LE.sr).toFixed(2)}s · ${LE.zc.length} zero-crossings · ${srcMsg}`;
      if (wasPlaying) lePlay();             // keep auditioning the new note immediately
    } catch (e) {
      document.getElementById('le-status').textContent = 'decode failed: ' + e.message;
    }
  }

  function drawLoopEditor() {
    const cv = document.getElementById('le-wave'); if (!cv || !LE.ch0) return;
    const W = cv.width, H = cv.height, g = cv.getContext('2d'), d = LE.ch0, n = d.length, mid = H / 2;
    if (LE.view.end <= LE.view.start) LE.view = { start: 0, end: n };   // default = full
    const vs = LE.view.start, ve = LE.view.end, vn = ve - vs;
    const xOf = (smp) => (smp - vs) / vn * W;                            // sample → canvas x (view-aware)
    g.clearRect(0, 0, W, H);
    const amp = LE.amp || 1;
    const clampY = (y) => y < 0 ? 0 : (y > H ? H : y);   // zoomed-in peaks clip at the canvas edge
    g.strokeStyle = '#e2e8f2'; g.lineWidth = 1; g.beginPath();   // bright trace — easy to see on black
    for (let x = 0; x < W; x++) {
      const i0 = vs + Math.floor(x / W * vn), i1 = Math.max(i0 + 1, vs + Math.floor((x + 1) / W * vn));
      let mn = 1, mx = -1;
      for (let i = i0; i < i1; i++) { if (d[i] < mn) mn = d[i]; if (d[i] > mx) mx = d[i]; }
      g.moveTo(x, clampY(mid - mx * mid * 0.95 * amp)); g.lineTo(x, clampY(mid - mn * mid * 0.95 * amp));
    }
    g.stroke();
    const xS = xOf(LE.S), xE = xOf(LE.E);
    g.fillStyle = 'rgba(242,183,5,0.10)'; g.fillRect(Math.max(0, xS), 0, Math.min(W, xE) - Math.max(0, xS), H);
    g.lineWidth = 2;
    if (xS >= 0 && xS <= W) { g.strokeStyle = '#3ad17a'; g.beginPath(); g.moveTo(xS, 0); g.lineTo(xS, H); g.stroke(); }
    if (xE >= 0 && xE <= W) { g.strokeStyle = '#ff5a5a'; g.beginPath(); g.moveTo(xE, 0); g.lineTo(xE, H); g.stroke(); }
    g.lineWidth = 1;
    const zoomTag = vn < n ? `  ·  zoom ${(n / vn).toFixed(1)}× (dbl-click to reset)` : '';
    document.getElementById('le-info').textContent =
      `loop ${(LE.S / LE.sr).toFixed(3)}s → ${(LE.E / LE.sr).toFixed(3)}s  (${((LE.E - LE.S) / LE.sr * 1000).toFixed(0)}ms · ${LE.E - LE.S} smp)${zoomTag}`;
    const ampEl = document.getElementById('le-amp-val'); if (ampEl) ampEl.textContent = `${(LE.amp || 1) % 1 ? (LE.amp).toFixed(1) : (LE.amp || 1)}×`;
    drawSeam();
  }

  function drawSeam() {
    const cv = document.getElementById('le-seam'); if (!cv || !LE.ch0) return;
    const W = cv.width, H = cv.height, g = cv.getContext('2d'), d = LE.ch0, mid = H / 2;
    g.clearRect(0, 0, W, H);
    const N = Math.min(300, LE.E - LE.S, LE.S);
    const amp = LE.amp || 1;
    const yOf = (v) => { const y = mid - v * mid * 0.9 * amp; return y < 0 ? 0 : (y > H ? H : y); };
    g.strokeStyle = '#e2e8f2'; g.lineWidth = 1.5; g.beginPath();   // bright seam trace
    for (let k = 0; k < N; k++) { const x = k / (2 * N) * W, v = d[LE.E - N + k] || 0; if (k === 0) g.moveTo(x, yOf(v)); else g.lineTo(x, yOf(v)); }
    for (let k = 0; k < N; k++) { const x = (N + k) / (2 * N) * W, v = d[LE.S + k] || 0; g.lineTo(x, yOf(v)); }
    g.stroke();
    g.strokeStyle = '#f2b705'; g.beginPath(); g.moveTo(W / 2, 0); g.lineTo(W / 2, H); g.stroke();
    const jump = Math.abs((d[LE.S] || 0) - (d[LE.E - 1] || 0));
    document.getElementById('le-seam-label').textContent = `seam Δ ${jump.toFixed(4)}`;
  }

  function lePlay() {
    leStop();
    if (!LE.ch0) return;
    const ac = ensureAudioContext();
    const sr = LE.sr;
    const S = Math.max(0, LE.S), E = Math.min(LE.ch0.length, LE.E);
    const region = E - S;
    if (region < 2) return;
    // Audition BACK-AND-FORTH (ping-pong) — exactly what the build ships, so no fake forward-loop
    // click. Buffer = [0..E] forward (attack + loop region) then the region reversed; looping
    // [S .. E+region] plays forward→reverse→forward = seamless ping-pong, just like the .adv.
    const src0 = LE.ch0;
    const ppLen = E + region;
    const buf = ac.createBuffer(1, ppLen, sr);
    const out = buf.getChannelData(0);
    out.set(src0.subarray(0, E), 0);
    for (let i = 0; i < region; i++) out[E + i] = src0[E - 1 - i];
    const src = ac.createBufferSource();
    src.buffer = buf; src.loop = true;
    src.loopStart = S / sr; src.loopEnd = ppLen / sr;
    src.connect(ac.destination); src.start(0, 0);
    LE.source = src; LE.playing = true;
  }
  function leStop() { if (LE.source) { try { LE.source.stop(); } catch (e) {} LE.source = null; } LE.playing = false; }

  function leSetHandle(idx) {
    idx = leSnap(idx);
    if (LE.dragging === 'S') LE.S = Math.max(0, Math.min(idx, LE.E - 64));
    else LE.E = Math.min(LE.ch0.length, Math.max(idx, LE.S + 64));
    drawLoopEditor();
    if (LE.playing && !LE.dragging) lePlay();  // rebuild ping-pong audition (snap/undo); drag rebuilds on mouseup
  }

  async function leLock() {
    const xf = parseInt(document.getElementById('le-xfade').value) || 0;
    try {
      const r = await fetch('/api/patch/loop-points', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instrument: LE.slug, patch: LE.patch, chain: LE.chain,
                               filename: LE.filename, start: LE.S, end: LE.E, crossfade: xf }),
      });
      const d = await r.json();
      document.getElementById('le-status').textContent = d.ok
        ? `🔒 locked ${LE.note.toUpperCase()} — ${d.total_set} note(s) set by ear. Run BUILD PACK to bake them in.`
        : 'lock failed: ' + d.error;
    } catch (e) { document.getElementById('le-status').textContent = 'lock error: ' + e.message; }
  }

  // wire editor controls once
  (function wireLoopEditor() {
    const wave = document.getElementById('le-wave'); if (!wave) return;
    // view-aware: map cursor x → sample index within the current zoom window
    const xToIdx = (clientX) => {
      if (!LE.ch0) return 0;
      const r = wave.getBoundingClientRect(); const x = (clientX - r.left) / r.width;
      const vs = LE.view.start, vn = (LE.view.end - LE.view.start) || LE.ch0.length;
      return Math.max(0, Math.min(LE.ch0.length - 1, Math.round(vs + x * vn)));
    };
    // ⌘+scroll / trackpad pinch → zoom around the cursor; double-click → reset to full
    wave.addEventListener('wheel', e => {
      if (!LE.ch0) return;
      if (e.shiftKey && !(e.ctrlKey || e.metaKey)) {  // shift+scroll = amplitude (vertical) zoom — get in on quiet detail
        e.preventDefault();
        LE.amp = Math.max(1, Math.min(64, (LE.amp || 1) * (e.deltaY < 0 ? 1.25 : 0.8)));
        drawLoopEditor();
        return;
      }
      if (!(e.ctrlKey || e.metaKey)) return;          // else cmd+scroll / pinch = time zoom
      e.preventDefault();
      const r = wave.getBoundingClientRect(); const mx = (e.clientX - r.left) / r.width;
      const vs = LE.view.start, vn = (LE.view.end - LE.view.start) || LE.ch0.length;
      const cursor = vs + mx * vn;
      const mult = Math.min(2, Math.max(0.5, Math.exp(e.deltaY * 0.0025)));   // up = zoom in
      let nvn = Math.max(256, Math.min(LE.ch0.length, vn * mult));
      let nvs = Math.round(cursor - mx * nvn);
      nvs = Math.max(0, Math.min(LE.ch0.length - Math.round(nvn), nvs));
      LE.view = { start: nvs, end: Math.round(nvs + nvn) };
      drawLoopEditor();
    }, { passive: false });
    wave.addEventListener('dblclick', e => { e.preventDefault(); LE.view = { start: 0, end: LE.ch0 ? LE.ch0.length : 0 }; drawLoopEditor(); });
    wave.addEventListener('mousedown', e => { if (!LE.ch0) return; lePushHistory(); const idx = xToIdx(e.clientX); LE.dragging = Math.abs(idx - LE.S) <= Math.abs(idx - LE.E) ? 'S' : 'E'; leSetHandle(idx); });
    window.addEventListener('mousemove', e => { if (LE.dragging) leSetHandle(xToIdx(e.clientX)); });
    window.addEventListener('mouseup', () => { const wasDrag = LE.dragging; LE.dragging = null; if (wasDrag && LE.playing) lePlay(); });
    document.getElementById('le-play').addEventListener('click', lePlay);
    document.getElementById('le-stop').addEventListener('click', leStop);
    document.getElementById('le-undo').addEventListener('click', leUndo);
    const leAmpSet = (v) => { LE.amp = Math.max(1, Math.min(64, v)); drawLoopEditor(); };
    document.getElementById('le-amp-in').addEventListener('click', () => leAmpSet((LE.amp || 1) * 2));
    document.getElementById('le-amp-out').addEventListener('click', () => leAmpSet((LE.amp || 1) / 2));
    document.getElementById('le-lock').addEventListener('click', leLock);
    document.getElementById('le-close').addEventListener('click', () => { leStop(); document.getElementById('loop-editor').style.display = 'none'; });
    document.getElementById('le-snap').addEventListener('change', () => { lePushHistory(); LE.S = leSnap(LE.S); LE.E = leSnap(LE.E); drawLoopEditor(); });
    const xf = document.getElementById('le-xfade');
    xf.addEventListener('input', () => { document.getElementById('le-xfade-val').textContent = xf.value; });
    // ⌘Z / Ctrl+Z undoes the last loop move (only while the editor is open)
    window.addEventListener('keydown', e => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z' &&
          document.getElementById('loop-editor').style.display === 'block') {
        e.preventDefault(); leUndo();
      }
    });
  })();

  async function drawWavTile(canvas, url) {
    const cctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    cctx.fillStyle = '#0a0a0a'; cctx.fillRect(0, 0, W, H);
    try {
      const ac = ensureAudioContext();
      const resp = await fetch(url);
      const buf = await resp.arrayBuffer();
      const audio = await ac.decodeAudioData(buf);
      const ch = audio.numberOfChannels > 1 ? mixToMono(audio) : audio.getChannelData(0);
      // Compute peak
      let peak = 0;
      for (let i = 0; i < ch.length; i++) {
        const a = Math.abs(ch[i]);
        if (a > peak) peak = a;
      }
      // Color by peak — silent flat gray, healthy amber, hot red
      let color;
      if (peak < 0.01) color = '#3a3a3a';
      else if (peak > 0.92) color = '#d65656';
      else color = '#F2B705';
      // Peak-per-pixel fill
      const samplesPerPx = Math.ceil(ch.length / W);
      cctx.fillStyle = color;
      cctx.shadowBlur = 4; cctx.shadowColor = color;
      for (let x = 0; x < W; x++) {
        let max = 0, min = 0;
        const start = x * samplesPerPx;
        const end = Math.min(ch.length, start + samplesPerPx);
        for (let i = start; i < end; i++) {
          if (ch[i] > max) max = ch[i];
          if (ch[i] < min) min = ch[i];
        }
        const yMax = H/2 - max * (H/2) * 0.85;
        const yMin = H/2 - min * (H/2) * 0.85;
        cctx.fillRect(x, yMax, 1, Math.max(1, yMin - yMax));
      }
      cctx.shadowBlur = 0;
      // dBFS overlay if silent
      if (peak < 0.01) {
        cctx.fillStyle = '#d65656';
        cctx.font = '9px monospace';
        cctx.fillText('SILENT', 4, 12);
      }
    } catch (e) {
      cctx.fillStyle = '#d65656';
      cctx.font = '9px monospace';
      cctx.fillText('error', 4, 12);
    }
  }

  // Zoom +/- buttons
  function rerenderStrip() {
    const strip = document.getElementById('patch-wavstrip');
    if (!strip) return;
    strip.querySelectorAll('.wav-tile').forEach(tile => {
      tile.style.width = `${wavstripTileWidth}px`;
      const canvas = tile.querySelector('.wav-tile-canvas');
      canvas.width = wavstripTileWidth * 2;
      // Redraw at new size
      const url = tile.dataset.wavUrl;
      drawWavTile(canvas, url);
    });
  }
  document.getElementById('patch-wavstrip-zoom-out')?.addEventListener('click', () => {
    wavstripTileWidth = Math.max(WAVSTRIP_MIN, wavstripTileWidth - 22);
    rerenderStrip();
  });
  document.getElementById('patch-wavstrip-zoom-in')?.addEventListener('click', () => {
    wavstripTileWidth = Math.min(WAVSTRIP_MAX, wavstripTileWidth + 22);
    rerenderStrip();
  });

  // BIND-FROM-SYNTH state — single in-flight session at a time
  let bindPollTimer = null;
  let bindCapturedPC = null;     // { pc, msb, lsb, channel, t } when captured
  let bindPatchData = null;      // patch we're binding to

  async function startBindFlow(patchData) {
    const settings = getAudioSettings();
    const midiPort = settings.midi_port;
    if (!midiPort) {
      setStatus('No MIDI port selected — pick an instrument first', 'error');
      return;
    }
    bindPatchData = patchData;
    bindCapturedPC = null;

    // Populate the overlay text + reveal
    const instrName = document.getElementById('instrument-select')?.selectedOptions[0]?.textContent || 'your synth';
    document.getElementById('bind-instrument-name').textContent = instrName;
    document.getElementById('bind-patch-name').textContent = patchData.name;
    const latestEl = document.getElementById('bind-latest-pc');
    latestEl.textContent = '— waiting for a Program Change —';
    latestEl.style.color = 'var(--fg-faint)';
    latestEl.style.borderLeftColor = 'var(--fg-faint)';
    document.getElementById('bind-confirm-btn').disabled = true;
    document.getElementById('patch-bind-overlay').style.display = 'block';

    // Start the backend listener
    try {
      const r = await fetch('/api/midi-listen/start', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ midi_port: midiPort }),
      });
      const d = await r.json();
      if (!d.ok) {
        setStatus(`Listen failed: ${d.error || 'unknown'}`, 'error');
        stopBindFlow();
        return;
      }
      setStatus(`🎧 Listening on ${d.port_name} — scroll the JP-8000 to YOUR version of "${patchData.name}"`, 'success');
    } catch (e) {
      setStatus(`Listen error: ${e.message}`, 'error');
      stopBindFlow();
      return;
    }

    // Poll for incoming PCs every 300ms. Keep the latest Bank Select MSB/LSB
    // we've seen (synths send Bank Select BEFORE PC), then bind PC + that bank.
    let lastMSB = null, lastLSB = null;
    bindPollTimer = setInterval(async () => {
      try {
        const r = await fetch('/api/midi-listen/poll');
        const d = await r.json();
        for (const e of (d.events || [])) {
          if (e.type === 'control_change' && e.control === 0) lastMSB = e.value;
          else if (e.type === 'control_change' && e.control === 32) lastLSB = e.value;
          else if (e.type === 'program_change') {
            // Captured! Show it + enable confirm.
            bindCapturedPC = { pc: e.value, msb: lastMSB, lsb: lastLSB, channel: e.channel, t: e.t };
            const bankStr = (lastMSB != null || lastLSB != null) ? ` · MSB=${lastMSB ?? '—'} · LSB=${lastLSB ?? '—'}` : '';
            const el = document.getElementById('bind-latest-pc');
            el.innerHTML = `<span style="color: var(--amber); font-weight: 700;">PC = ${e.value}</span>${bankStr}<div style="font-family: var(--mono); font-size: 11px; color: var(--fg-dim); margin-top: 6px;">ch${e.channel} — ready to bind to "${bindPatchData.name}"</div>`;
            el.style.color = 'var(--fg)';
            el.style.borderLeftColor = 'var(--amber)';
            document.getElementById('bind-confirm-btn').disabled = false;
            // Reset bank tracking — the next PC could be a different bank
            lastMSB = null; lastLSB = null;
          }
        }
      } catch (e) { /* swallow poll errors — keep trying */ }
    }, 300);
  }

  async function stopBindFlow() {
    if (bindPollTimer) { clearInterval(bindPollTimer); bindPollTimer = null; }
    try { await fetch('/api/midi-listen/stop', { method: 'POST' }); } catch (e) { /* noop */ }
    document.getElementById('patch-bind-overlay').style.display = 'none';
    bindCapturedPC = null;
    bindPatchData = null;
  }

  // Wire confirm + cancel buttons once at module load — they reference module-scoped state
  document.getElementById('bind-confirm-btn')?.addEventListener('click', async () => {
    if (!bindCapturedPC || !bindPatchData) return;
    const instrSlug = document.getElementById('instrument-select')?.value;
    if (!instrSlug) return;
    const payload = {
      program_change: bindCapturedPC.pc,
      bank_msb: bindCapturedPC.msb,
      bank_lsb: bindCapturedPC.lsb,
    };
    try {
      const r = await fetch(`/api/instruments/${instrSlug}/patches/${encodeURIComponent(bindPatchData.name)}/bind-pc`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload),
      });
      const d = await r.json();
      if (d.ok) {
        const bankStr = (payload.bank_msb != null || payload.bank_lsb != null) ? ` (MSB=${payload.bank_msb}, LSB=${payload.bank_lsb})` : '';
        setStatus(`✓ Bound "${bindPatchData.name}" → PC=${payload.program_change}${bankStr}`, 'success');
        // Refresh the iconic patches list so the row shows the new PC badge
        const slug = document.getElementById('instrument-select')?.value;
        if (slug) loadIconicPatches(slug);
      } else {
        setStatus(`Bind failed: ${d.error || 'unknown'}`, 'error');
      }
    } catch (e) {
      setStatus(`Bind error: ${e.message}`, 'error');
    }
    stopBindFlow();
  });

  document.getElementById('bind-cancel-btn')?.addEventListener('click', () => {
    setStatus('Bind canceled.', '');
    stopBindFlow();
  });

  // Load the iconic-patches roadmap for the current instrument. Shows manifest.patches
  // with ✓ captured / ◐ pending / ○ todo status. Each row is now a TWO-IN-ONE click:
  //   - if the patch is linked to a gearbase preset (has PC + Bank), fires MIDI
  //     Program Change + Bank Select → synth's display physically jumps to it
  //   - then loads the patch name into the multisample capture form so you can
  //     immediately hit "Capture" to multisample the sound you just summoned.
  // Also: appends a collapsible "📋 Factory presets" section underneath listing all
  // ~128 official presets from data/gearbase/presets/<slug>.json (also click-to-load).
  async function loadIconicPatches(slug) {
    const listEl = document.getElementById('iconic-patches-list');
    const summaryEl = document.getElementById('iconic-summary');
    if (!listEl) return;
    try {
      const _bust = Date.now();
      const [pr, fr] = await Promise.all([
        fetch(`/api/instruments/${slug}/patches?t=${_bust}`),
        fetch(`/api/instruments/${slug}/factory-presets?t=${_bust}`),
      ]);
      if (!pr.ok) {
        listEl.innerHTML = '<div style="color: var(--fg-faint);">(no patches scaffolded yet)</div>';
        summaryEl.textContent = '';
        return;
      }
      const data = await pr.json();
      const factory = fr.ok ? await fr.json() : { presets: [] };
      // Build the slug -> real-name map so the captures log + session report show
      // "A14 · Juno Sub Bass" instead of the "sub-bass" folder slug.
      data.patches.forEach(p => {
        if (p.preset_name) presetNames[p.name] = `${p.preset_position ? p.preset_position + ' · ' : ''}${p.preset_name}`;
      });
      // The name map is populated NOW. Redraw the captures log immediately so a hard refresh shows
      // the real preset names right away — without this it renders the raw folder slugs (the "old
      // names") until the 10s poll catches up. (Both live in this closure, so the call is in scope.)
      if (typeof loadCapturesLog === 'function') loadCapturesLog();
      // Populate the capture "Patch" dropdown from the manifest — so a capture can never
      // land as "untitled" again (free-text default was the footgun). Preserves selection.
      const msPatchSel = document.getElementById('ms-patch');
      if (msPatchSel && msPatchSel.tagName === 'SELECT' && data.patches.length) {
        const cur = msPatchSel.value;
        msPatchSel.innerHTML = data.patches.map(p => {
          const label = p.preset_name ? `${p.preset_position ? p.preset_position + ' · ' : ''}${p.preset_name}` : p.name;
          return `<option value="${p.name}">${label}</option>`;
        }).join('');
        if (cur && data.patches.some(p => p.name === cur)) msPatchSel.value = cur;
      }
      // Fold the full factory bank into data.patches as PENDING (todo) stubs alongside your captured
      // ones, so the click/capture handler finds any of them by name. The RENDER below keeps YOUR
      // patches grouped at the top + groups the rest into collapsible CATEGORY drawers (bass/lead/
      // pad/...). Sorted by slot so each category reads A11->B88. A pending preset fills the NEW-name box.
      {
        const capIds = new Set(data.patches.map(p => p.gearbase_preset_id || (p.preset_position ? 'P:' + p.preset_position : '')).filter(Boolean));
        const pending = (factory.presets || []).filter(fp => fp.id && !capIds.has(fp.id)).map(fp => {
          // Auto-name a new capture its ORIGINAL preset name (slot · name), mirroring the synth
          // screen — never "hook-…". The capture folder follows this name.
          const pos = (fp.id || '').replace(/^P:/, '');
          return { name: `${pos} · ${fp.name}`, preset_name: fp.name, preset_position: pos,
            program_change: fp.program_change, bank_msb: fp.bank_msb, bank_lsb: fp.bank_lsb,
            role: fp.category || '', notes: '', status: 'todo', gearbase_preset_id: fp.id, _pending: true };
        });
        const slotKey = (p) => p.gearbase_preset_id || ('P:' + (p.preset_position || ''));
        data.patches = data.patches.concat(pending).sort((a, b) => slotKey(a) < slotKey(b) ? -1 : slotKey(a) > slotKey(b) ? 1 : 0);
        const capN = data.patches.filter(p => p.status === 'captured').length;
        data.summary = { captured: capN, pending: 0, todo: data.patches.length - capN, total: data.patches.length };
        capturedPatches = {}; data.patches.forEach(p => { if (p.status === 'captured' && p.wav_count) capturedPatches[p.name] = p.wav_count; });
      }
      const s = data.summary;
      const _allCaps = data.patches.map(p => p.captured_at).filter(Boolean);
      const _fresh = _allCaps.length ? Math.max(..._allCaps) : 0;
      const _staleN = data.patches.filter(p => p.captured_at && _fresh && (_fresh - p.captured_at) > 40 * 60).length;
      const staleBit = _staleN ? ` · <b style="color: var(--amber);">${_staleN}</b> old take${_staleN > 1 ? 's' : ''} to recapture` : '';
      summaryEl.innerHTML = `<b style="color: var(--green);">${s.captured}</b> captured · <b style="color: var(--amber);">${s.pending}</b> pending · <b>${s.todo}</b> todo · <b>${s.total}</b> total${staleBit}`;

      const roleColors = { lead: '#F2B705', bass: '#7AA2F7', pad: '#9D7CD8', keys: '#73DACA', synth: '#8A8FA3', drums: '#F7768E', arp: '#E0AF68' };
      // Freshest capture across the set = the current pass. Patches captured well before
      // it (>40 min gap) are leftover OLD takes from a previous session — flag them so a
      // rebuild never silently blends old + new, and you can see re-capture progress.
      const _caps = data.patches.map(p => p.captured_at).filter(Boolean);
      const freshest = _caps.length ? Math.max(..._caps) : 0;
      const ago = (secs) => {
        const m = secs / 60;
        return m < 90 ? `${Math.round(m)}m ago` : `${(m / 60).toFixed(1)}h ago`;
      };
      // Render one patch row — reused for your iconic patches AND the factory-preset categories.
      const renderRow = (p) => {
        const stale = p.captured_at && freshest && (freshest - p.captured_at) > 40 * 60;
        const icon = p.status === 'captured' ? (stale ? '⟳' : '✓') : p.status === 'pending' ? '◐' : '○';
        const color = stale ? 'var(--amber)' : p.status === 'captured' ? 'var(--green)' : p.status === 'pending' ? 'var(--amber)' : 'var(--fg-faint)';
        const ageTxt = p.captured_at ? ` <span style="color:${stale ? 'var(--amber)' : 'var(--fg-faint)'}; font-size:9px;">· ${stale ? 'OLD take · ' : ''}${ago((Date.now()/1000) - p.captured_at)}</span>` : '';
        const wavInfo = p.status === 'captured' ? ` <span style="color: var(--fg-faint); font-size: 10px;">(${p.wav_count} wav)${ageTxt}</span>` : '';
        const notesTrim = (p.notes || '').slice(0, 90) + ((p.notes || '').length > 90 ? '…' : '');
        const notesEscaped = (p.notes || '').replace(/"/g, '&quot;');
        const heroName = p.preset_name ? `${p.preset_position ? p.preset_position + ' · ' : ''}${p.preset_name}` : p.name;
        const slugTag = (p.preset_name && p.name !== p.preset_name) ? `<span style="color: var(--fg-faint); font-family: var(--mono); font-size: 9px; opacity: 0.75;">${p.name}</span> · ` : '';
        const rc = roleColors[p.role];
        const roleChip = p.role ? `<span style="background: ${rc || 'var(--bg-2)'}22; color: ${rc || 'var(--fg-faint)'}; border: 1px solid ${rc || 'var(--border)'}55; padding: 0 5px; border-radius: 2px; font-size: 8px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; margin-left: 6px; vertical-align: middle;">${p.role}</span>` : '';
        const pcBadge = (p.program_change != null) ? `<span style="background: var(--grad-amber); color: #1a0e00; padding: 1px 6px; border-radius: 2px; font-family: var(--mono); font-size: 9px; font-weight: 700; letter-spacing: 0.05em; margin-left: 6px; vertical-align: middle;" title="Click row to send PC ${p.program_change}">PC ${p.program_change}</span>` : '';
        return `
          <div class="iconic-patch-row" data-patch="${p.name}" data-pc="${p.program_change ?? ''}" data-msb="${p.bank_msb ?? ''}" data-lsb="${p.bank_lsb ?? ''}" data-preset-id="${p.gearbase_preset_id || ''}" title="${notesEscaped}" style="display: flex; gap: 10px; padding: 9px 10px; background: var(--bg-3); border-left: 3px solid ${color}; cursor: pointer; transition: all 0.12s ease;" onmouseover="this.style.background='var(--bg-2)';this.style.transform='translateX(2px)'" onmouseout="this.style.background='var(--bg-3)';this.style.transform=''">
            <span style="color: ${color}; font-weight: 700; min-width: 14px; font-size: 14px; line-height: 1;">${icon}</span>
            <div style="flex: 1; min-width: 0;">
              <div style="font-family: var(--display); color: var(--fg); font-weight: 600; font-size: 13px; letter-spacing: 0.02em;">${heroName}${roleChip}${pcBadge}${wavInfo}</div>
              <div style="color: var(--fg-faint); font-size: 10px; line-height: 1.4; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${slugTag}${notesTrim}</div>
            </div>
          </div>`;
      };

      // YOUR patches split into the curated ICONICS (foldable once you're done with them) and the
      // NEW factory captures you've been grabbing (kept visible while you work).
      const mine = data.patches.filter(p => !p._pending);
      const iconics = mine.filter(p => p.source !== 'factory-capture');
      const newCaps = mine.filter(p => p.source === 'factory-capture');
      const iconHtml = iconics.length ? iconics.map(renderRow).join('')
        : '<div style="color: var(--fg-faint); padding: 8px;">(no patches yet — add to manifest.patches[])</div>';
      const newHtml = newCaps.map(renderRow).join('');

      // The rest of the factory bank, grouped by CATEGORY in collapsible drawers — pop open
      // "bass" / "lead" / "pad" to grab a few more to capture.
      const byCat = {};
      data.patches.filter(p => p._pending).forEach(p => {
        const c = (p.role || 'other').toLowerCase();
        (byCat[c] = byCat[c] || []).push(p);
      });
      const CAT_ORDER = ['bass', 'lead', 'pad', 'keys', 'synth', 'arp', 'pluck', 'fx', 'other'];
      const cats = Object.keys(byCat).sort((a, b) =>
        ((CAT_ORDER.indexOf(a) + 1 || 99) - (CAT_ORDER.indexOf(b) + 1 || 99)) || a.localeCompare(b));
      const catHtml = cats.map(c => `
        <details style="margin-top: 6px; background: var(--bg-3); border: 1px solid var(--border); border-radius: 2px;">
          <summary style="padding: 7px 10px; cursor: pointer; font-size: 11px; font-weight: 700; color: var(--fg); text-transform: capitalize; letter-spacing: 0.04em;">${c} <span style="color: var(--fg-faint); font-weight: 400;">(${byCat[c].length})</span></summary>
          <div style="max-height: 320px; overflow-y: auto; border-top: 1px solid var(--border);">${byCat[c].map(renderRow).join('')}</div>
        </details>`).join('');

      const _hdr = (t) => `<div style="font-size: 10px; font-weight: 700; color: var(--fg-faint); text-transform: uppercase; letter-spacing: 0.1em; padding: 8px 2px 4px;">${t}</div>`;
      const iconOpen = localStorage.getItem('benchIconicsFold') !== 'closed';   // remembers your fold across refreshes
      listEl.innerHTML =
        `<details ${iconOpen ? 'open' : ''} data-iconics style="margin-top: 4px; background: var(--bg-3); border: 1px solid var(--border); border-radius: 2px;">`
        + `<summary style="padding: 7px 10px; cursor: pointer; font-size: 11px; font-weight: 700; color: var(--fg); letter-spacing: 0.04em;">★ iconic patches <span style="color: var(--fg-faint); font-weight: 400;">(${iconics.length}) — click to fold</span></summary>`
        + `<div style="border-top: 1px solid var(--border);">${iconHtml}</div></details>`
        + (newCaps.length ? _hdr(`new captures (${newCaps.length})`) + newHtml : '')
        + (catHtml ? _hdr('more from the factory bank — by category') + catHtml : '');
      const _icd = listEl.querySelector('details[data-iconics]');
      if (_icd) _icd.addEventListener('toggle', () => localStorage.setItem('benchIconicsFold', _icd.open ? 'open' : 'closed'));

      // Helper: send PC + Bank Select to the currently-selected instrument's MIDI port
      async function fireLoadPatch(name, pc, msb, lsb, presetId) {
        const settings = getAudioSettings();
        const midiPort = settings.midi_port;
        if (!midiPort) {
          setStatus('No MIDI port selected — pick an instrument first', 'error');
          return false;
        }
        if (pc === '' || pc == null) return false;  // not linked, nothing to fire
        // BANK SELECT: only sent if we captured real MSB/LSB from the synth's own
        // broadcast via the bind-from-synth flow. Patches that were hand-linked
        // (with potentially wrong MSB=80 guess) get bare PC instead. This keeps
        // the JP-8000 from getting confused by invalid bank values.
        const body = {
          midi_port: midiPort,
          channel: 1,
          program_change: parseInt(pc, 10),
          patch_name: name,
        };
        if (msb !== '' && msb != null) body.bank_msb = parseInt(msb, 10);
        if (lsb !== '' && lsb != null) body.bank_lsb = parseInt(lsb, 10);
        try {
          const r = await fetch('/api/load-patch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          const d = await r.json();
          if (d.ok) {
            setStatus(`🎛 ${presetId ? presetId+' · ' : ''}${name} → ${d.midi_port} (${d.messages_sent.join(' · ')})`, 'success');
            return true;
          } else {
            setStatus(`Load failed: ${d.error || 'unknown'}`, 'error');
            return false;
          }
        } catch (e) {
          setStatus(`Load error: ${e.message}`, 'error');
          return false;
        }
      }

      // Wire iconic-patch row clicks — fires PC if linked + pulls up patch detail card
      // + loads name into capture form so Capture button is ready to fire
      listEl.querySelectorAll('.iconic-patch-row').forEach(row => {
        row.addEventListener('click', async () => {
          const patchData = data.patches.find(p => p.name === row.dataset.patch);
          if (!patchData) return;
          const pc = row.dataset.pc;
          const msb = row.dataset.msb;
          const lsb = row.dataset.lsb;
          const presetId = row.dataset.presetId;

          // Visual: highlight selected row
          listEl.querySelectorAll('.iconic-patch-row').forEach(r => r.style.outline = '');
          row.style.outline = '2px solid var(--amber)';

          // Fire PC if linked
          if (pc !== '') {
            await fireLoadPatch(patchData.name, pc, msb, lsb, presetId);
          }

          // Pull up the patch detail card — beautiful big presentation of what's selected
          showPatchDetail(patchData, presetId);

          // Remember the sound now loaded on the synth — it gets stamped onto any progression you fire.
          currentPreset = {
            name: patchData.name,
            display: patchData.preset_name ? `${patchData.preset_position ? patchData.preset_position + ' · ' : ''}${patchData.preset_name}` : patchData.name,
          };
          try { localStorage.setItem('benchCurrentPreset', JSON.stringify(currentPreset)); } catch (e) {}
          updatePresetReadout();

          // Pre-fill the capture NAME so it's ready — but DON'T force the capture style. If you're on
          // Chord progression (or any mode), clicking a preset just loads it; you stay where you are.
          // (The detail card's "capture" button is the deliberate switch-to-multisample action.)
          const sel = document.getElementById('ms-patch');
          const neu = document.getElementById('ms-patch-new');
          if (patchData._pending) {
            if (neu) neu.value = patchData.name;
            // Remember the factory slot so the next capture links to it (marks the slot ✓ +
            // ties the multisamples to it) even if you rename the patch "hook-…".
            captureTarget = {
              preset_name: patchData.preset_name, preset_position: patchData.preset_position,
              gearbase_preset_id: patchData.gearbase_preset_id, program_change: patchData.program_change,
              bank_msb: patchData.bank_msb, bank_lsb: patchData.bank_lsb, role: patchData.role,
            };
          } else if (sel) {
            sel.value = patchData.name;
            if (neu) neu.value = '';
            captureTarget = null;   // already a real manifest patch — no factory link to ride along
          }
        });
      });
      updatePresetReadout();   // reflect the loaded sound in the Play Along block

    } catch (e) {
      listEl.innerHTML = `<div style="color: var(--red, #c44);">error: ${e.message}</div>`;
    }
  }

  // ---------- capture ----------
  // Auto-name a capture from the patch you've got loaded (the Patch dropdown) when you
  // haven't typed a real name — so a take is never anonymous "untitled-X". A name you type wins.
  function autoName(fieldId, untitledDefault) {
    const typed = (document.getElementById(fieldId)?.value || '').trim();
    if (typed && typed !== untitledDefault) return typed;
    const patch = (document.getElementById('ms-patch')?.value || '').trim();
    return patch || untitledDefault;
  }
  function gatherParams() {
    const instrument = els.instrumentSelect.value;
    const settings = getAudioSettings();
    const base = { style: currentStyle, instrument, ...settings };
    if (currentStyle === 'progression') {
      return { ...base,
        name: autoName('prog-name', 'untitled-prog'),
        progression: document.getElementById('prog-chords').value.trim() || 'Cm Ab Eb Bb',
        bpm: parseFloat(document.getElementById('prog-bpm').value) || 120,
        bars_per_chord: parseFloat(document.getElementById('prog-bpc').value) || 2,
        send_mode: document.getElementById('prog-send-mode')?.value || 'chord',
        preset: currentPreset?.display || '',        // the sound this progression was played on
        preset_name: currentPreset?.name || '',
      };
    }
    if (currentStyle === 'multisample') {
      const newName = document.getElementById('ms-patch-new')?.value.trim() || '';
      // Factory-slot link rides along ONLY when capturing via the new-name box (the catalog flow)
      // with a preset clicked — so it marks that slot ✓ + ties the multisamples to it.
      const link = (newName && captureTarget) ? captureTarget : {};
      return { ...base,
        patch: (newName || document.getElementById('ms-patch').value.trim() || 'untitled'),
        chain: document.getElementById('ms-chain').value,
        note_range: document.getElementById('ms-range').value.trim() || 'C2-C5',
        step: parseInt(document.getElementById('ms-step').value) || 3,
        velocities: document.getElementById('ms-vel').value,
        sustain_sec: parseFloat(document.getElementById('ms-sustain').value) || 4,
        tail_sec: 2.5,
        take: document.getElementById('ms-take')?.checked || false,   // continuous take + slice
        ...link,
      };
    }
    if (currentStyle === 'hihat') {
      return { ...base,
        name: document.getElementById('hihat-name').value.trim() || 'untitled-hihat',
        target_bpm: parseFloat(document.getElementById('hihat-bpm').value) || 140,
      };
    }
    if (currentStyle === 'sweep') {
      return { ...base,
        name: autoName('sweep-name', 'untitled-sweep'),
        duration: parseFloat(document.getElementById('sweep-dur').value) || 8,
      };
    }
    if (currentStyle === 'drums') {
      return { ...base,
        pattern: document.getElementById('drums-pattern').value || 'four-on-floor',
        bars: parseInt(document.getElementById('drums-bars').value) || 4,
        bpm: parseFloat(document.getElementById('drums-bpm').value) || undefined,
        midi_port: document.getElementById('drums-port').value.trim() || 'TR-808',
        midi_channel: parseInt(document.getElementById('drums-channel').value) || 10,
      };
    }
    return base;
  }

  function setStatus(text, cls = '') {
    els.status.textContent = text;
    els.status.className = 'status ' + cls;
  }

  els.captureBtn.addEventListener('click', async () => {
    const params = gatherParams();
    // OVERWRITE GUARD — a multisample capture into a patch that already has samples, with no fresh
    // name typed, would silently clobber it (the 1979 Performance → supersaw-1 bug). Confirm first.
    if (params.style === 'multisample' && capturedPatches[params.patch]) {
      const ok = confirm(`"${params.patch}" already has ${capturedPatches[params.patch]} samples.\n\nCapturing now OVERWRITES them. If this is a different sound, hit Cancel and type a name in the "New name" box first.\n\nOverwrite "${params.patch}"?`);
      if (!ok) return;
    }
    setStatus('Capturing… click ■ Stop to abort.', 'busy');
    els.captureBtn.disabled = true;
    els.captureBtn.style.display = 'none';
    document.getElementById('capture-stop-btn').style.display = 'inline-block';
    els.playback?.classList.remove('visible');

    try {
      const r = await fetch('/api/capture', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      });
      const data = await r.json();
      els.consoleCard.style.display = 'block';
      els.consoleEl.textContent = data.stdout_tail || data.stderr || JSON.stringify(data, null, 2);

      if (!data.ok) {
        setStatus(`Capture failed: ${data.error || 'see console'}`, 'error');
        return;
      }

      // Refresh status for ANY successful capture — a multisample may not return a
      // single wav_url, but the checklist must still flip the row ○ → ✓.
      loadInstrument(params.instrument);
      loadIconicPatches(params.instrument);

      if (data.wav_url) {
        lastCapture = { ...data, params };
        // Multisample captures write many files; show the count so the user sees it
        const msMsg = data.multisample_count
          ? `✓ Captured ${data.multisample_count} samples → ${data.multisample_dir || data.wav_path.split('/').slice(0,-1).join('/')}`
          : `✓ Captured: ${data.wav_path.split('/').pop()}`;
        setStatus(msMsg, 'success');
        // refresh instrument progress + iconic patches list so the row flips ○ → ✓
        loadInstrument(params.instrument);
        loadIconicPatches(params.instrument);
      } else {
        setStatus('Capture completed but no WAV file was located. Check console.', 'error');
      }
    } catch (e) {
      setStatus(`Error: ${e.message}`, 'error');
    } finally {
      els.captureBtn.disabled = false;
      els.captureBtn.style.display = 'inline-block';
      document.getElementById('capture-stop-btn').style.display = 'none';
    }
  });

  // ---------- stop capture mid-run ----------
  document.getElementById('capture-stop-btn').addEventListener('click', async () => {
    const settings = getAudioSettings();
    setStatus('Stopping capture + sending MIDI panic…', 'busy');
    try {
      const r = await fetch('/api/capture-stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ midi_port: settings.midi_port }),
      });
      const data = await r.json();
      if (data.ok) {
        setStatus(`✓ Capture stopped${data.killed_process ? ' (process terminated)' : ' (nothing was running)'} + MIDI panic sent.`, 'success');
      } else {
        setStatus(`Stop failed: ${data.error}`, 'error');
      }
    } catch (e) {
      setStatus(`Stop error: ${e.message}`, 'error');
    } finally {
      // ALWAYS restore the UI — this was the bug: Stop sent the request but left the
      // button stuck on "capturing" forever. Now Stop truly resets to idle.
      els.captureBtn.disabled = false;
      els.captureBtn.style.display = 'inline-block';
      document.getElementById('capture-stop-btn').style.display = 'none';
    }
  });

  // (Playback card removed — multisample captures land in the captures log + checklist;
  // trim/gain/loudness are automatic at capture + build, so keep/discard/retake/trim are gone.)

  // ---------- saved progressions ----------
  let allProgressions = [];
  async function loadSavedProgressions() {
    try {
      const r = await fetch('/api/patterns/progressions');
      allProgressions = await r.json();
      renderGenreChips();
      renderProgressionDropdown();
    } catch (e) {
      console.error('failed to load saved progressions', e);
    }
  }

  // primary genres (subset of all tags) — surface as chip buttons
  const GENRE_CHIPS = ['house', 'hip-hop', 'rnb', 'dub', 'soul', 'jazz', 'trap', 'lofi', 'disco', 'dnb', 'uk-garage', 'reggaeton', 'afrobeat'];
  let activeGenre = null;

  function renderGenreChips() {
    const wrap = document.getElementById('prog-genre-chips');
    if (!wrap) return;
    wrap.innerHTML = '';
    const allBtn = document.createElement('button');
    allBtn.textContent = 'all';
    allBtn.style.cssText = 'background: ' + (activeGenre ? 'var(--bg-3)' : 'var(--amber)') + '; color: ' + (activeGenre ? 'var(--fg-dim)' : '#000') + '; padding: 6px 12px; font-size: 11px; border: none; cursor: pointer; letter-spacing: 0.05em;';
    allBtn.addEventListener('click', () => { activeGenre = null; renderGenreChips(); renderProgressionDropdown(); });
    wrap.appendChild(allBtn);
    GENRE_CHIPS.forEach(g => {
      // count how many patterns have this tag
      const count = allProgressions.filter(p => (p.tags || []).some(t => t.toLowerCase() === g)).length;
      if (count === 0) return;
      const btn = document.createElement('button');
      btn.textContent = `${g} (${count})`;
      const isActive = activeGenre === g;
      btn.style.cssText = 'background: ' + (isActive ? 'var(--amber)' : 'var(--bg-3)') + '; color: ' + (isActive ? '#000' : 'var(--fg-dim)') + '; padding: 6px 12px; font-size: 11px; border: none; cursor: pointer; letter-spacing: 0.05em;';
      btn.addEventListener('click', () => {
        activeGenre = isActive ? null : g;
        renderGenreChips();
        renderProgressionDropdown();
      });
      wrap.appendChild(btn);
    });
  }

  // Parse a key string like "Cm", "C#m", "Am", "Cmaj7", "Ebmin" → { root: 'c', mode: 'minor' }
  function parseKey(keyStr) {
    if (!keyStr) return { root: '', mode: '' };
    const s = String(keyStr).toLowerCase().trim();
    const m = s.match(/^([a-g][#b]?)(.*)$/);
    if (!m) return { root: '', mode: '' };
    let root = m[1].replace('b', '#'); // we don't care about enharmonic in filter
    // remap flats to sharps for filter matching
    const flatMap = { 'a#': 'a#', 'c#': 'c#', 'd#': 'd#', 'f#': 'f#', 'g#': 'g#' };
    // crude: if user typed 'eb' it became 'e#' — undo and snap to canonical
    const flatToSharp = { 'a#': 'a#', 'b#': 'c', 'd#': 'd#', 'e#': 'f', 'g#': 'g#' };
    // Actually simpler: just compare root letter+sharp/flat literal
    const rest = m[2].trim();
    let mode = '';
    if (/^(m|min|minor)/.test(rest)) mode = 'minor';
    else if (/^(maj|major|M)/.test(rest)) mode = 'major';
    else mode = ''; // unknown — leave blank, won't match mode filter
    return { root: m[1], mode };
  }

  // Populate the style/genre dropdown dynamically from all unique tags in the library.
  // Common-ish tags surface first; one-off tags get folded under "—".
  function populateGenreDropdown() {
    const sel = document.getElementById('prog-filter-genre');
    if (!sel || !allProgressions.length) return;
    const tagCount = {};
    allProgressions.forEach(p => {
      (p.tags || []).forEach(t => { tagCount[t] = (tagCount[t] || 0) + 1; });
    });
    // Sort by count desc, only show tags that appear in 2+ patterns (the others are noise)
    const sorted = Object.entries(tagCount)
      .filter(([_, n]) => n >= 2)
      .sort((a, b) => b[1] - a[1]);
    // Keep the current selection if any
    const current = sel.value;
    sel.innerHTML = '<option value="">all styles</option>';
    sorted.forEach(([tag, n]) => {
      const opt = document.createElement('option');
      opt.value = tag;
      opt.textContent = `${tag} (${n})`;
      sel.appendChild(opt);
    });
    if (current) sel.value = current;
  }

  function renderProgressionDropdown() {
    const sel = document.getElementById('prog-saved');
    const filterText = (document.getElementById('prog-filter')?.value || '').toLowerCase().trim();
    const tempoFilter = document.getElementById('prog-filter-tempo')?.value || '';
    const rootFilter = (document.getElementById('prog-filter-root')?.value || '').toLowerCase();
    const modeFilter = (document.getElementById('prog-filter-mode')?.value || '').toLowerCase();
    const genreFilter = (document.getElementById('prog-filter-genre')?.value || '').toLowerCase();

    let [loBpm, hiBpm] = [0, 999];
    if (tempoFilter) [loBpm, hiBpm] = tempoFilter.split('-').map(Number);

    const filtered = allProgressions.filter(p => {
      const tagStr = (p.tags || []).join(' ').toLowerCase();
      const vibeStr = (p.vibe || '').toLowerCase();
      const nameStr = (p.name || '').toLowerCase();
      const { root: pRoot, mode: pMode } = parseKey(p.key);

      // Legacy chip system still works if anything sets activeGenre
      if (activeGenre && !(p.tags || []).some(t => t.toLowerCase() === activeGenre)) return false;
      // Genre dropdown
      if (genreFilter && !(p.tags || []).some(t => t.toLowerCase() === genreFilter)) return false;
      // Free text — across tags, vibe, name
      if (filterText && !tagStr.includes(filterText) && !vibeStr.includes(filterText) && !nameStr.includes(filterText)) return false;
      // Tempo bucket
      if (tempoFilter && (p.bpm < loBpm || p.bpm > hiBpm)) return false;
      // Root note
      if (rootFilter && pRoot !== rootFilter) return false;
      // Mode (minor / major)
      if (modeFilter && pMode !== modeFilter) return false;
      return true;
    });

    sel.innerHTML = `<option value="">— ${filtered.length} of ${allProgressions.length} progressions —</option>`;
    filtered.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.name;
      opt.dataset.chords = (p.chords || []).join(' ');
      opt.dataset.bpm = p.bpm || 120;
      opt.dataset.bpc = p.bars_per_chord || 2;
      opt.dataset.vibe = p.vibe || '';
      const tags = (p.tags || []).slice(0, 3).join(' · ');
      opt.textContent = `${p.name}  ·  ${p.bpm}bpm · ${p.key || ''}  ${tags ? '· ' + tags : ''}`;
      sel.appendChild(opt);
    });
  }

  // wire up filter inputs (all of them — any change re-filters live)
  ['prog-filter', 'prog-filter-tempo', 'prog-filter-root', 'prog-filter-mode', 'prog-filter-genre'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', renderProgressionDropdown);
    if (el) el.addEventListener('change', renderProgressionDropdown);
  });
  // Clear-all button
  document.getElementById('prog-filter-clear')?.addEventListener('click', () => {
    ['prog-filter', 'prog-filter-tempo', 'prog-filter-root', 'prog-filter-mode', 'prog-filter-genre'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
    activeGenre = null;
    renderProgressionDropdown();
  });
  // Populate genre dropdown once data arrives
  const _origRenderGenreChips = typeof renderGenreChips === 'function' ? renderGenreChips : null;
  const _wireDropdownsAfterLoad = setInterval(() => {
    if (allProgressions && allProgressions.length > 0) {
      populateGenreDropdown();
      clearInterval(_wireDropdownsAfterLoad);
    }
  }, 300);

  document.getElementById('prog-saved').addEventListener('change', () => {
    const sel = document.getElementById('prog-saved');
    const opt = sel.options[sel.selectedIndex];
    if (!opt.value) {
      document.getElementById('prog-saved-vibe').textContent = '';
      return;
    }
    document.getElementById('prog-name').value = opt.value;
    document.getElementById('prog-chords').value = opt.dataset.chords;
    document.getElementById('prog-bpm').value = opt.dataset.bpm;
    document.getElementById('prog-saved-vibe').textContent = opt.dataset.vibe;
    // Set loop length from the pattern's saved bpc × chord count
    const patternBpc = parseFloat(opt.dataset.bpc) || 2;
    const nChordsInPattern = opt.dataset.chords.split(/\s+/).length;
    const patternLoopBars = patternBpc * nChordsInPattern;
    const loopLenEl = document.getElementById('prog-loop-length');
    if (loopLenEl) {
      // pick the closest supported loop length (4, 8, 16)
      const supported = [4, 8, 16];
      const closest = supported.reduce((a, b) =>
        Math.abs(b - patternLoopBars) < Math.abs(a - patternLoopBars) ? b : a);
      loopLenEl.value = String(closest);
    }
    document.getElementById('prog-bpc').value = patternBpc;
    // Pattern's BPM becomes the global clock BPM — click + synth follow.
    const patternBpm = parseFloat(opt.dataset.bpm) || 120;
    const clockBpm = document.getElementById('clock-bpm');
    if (clockBpm) {
      clockBpm.value = patternBpm;
      // POST live + status feedback
      fetch('/api/clock/bpm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bpm: patternBpm }),
      }).catch(() => {});
      setStatus(`🔒 Locked to ${opt.value} @ ${patternBpm} BPM`, 'success');
    }
    // refresh the loop-length readout with the new chord count + BPM + BPC
    if (typeof updateProgLengthReadout === 'function') updateProgLengthReadout();
  });

  // ---------- progression loop-length readout ----------
  // Loop length is the PRIMARY control (4 / 8 / 16 bars). bars-per-chord (bpc) is
  // computed from loop_length / n_chords. The math has to come out to a whole or
  // half number — otherwise the readout goes red and warns "doesn't fit cleanly."
  function recomputeBpcFromLoopLength() {
    const loopLenEl = document.getElementById('prog-loop-length');
    const chordsEl = document.getElementById('prog-chords');
    const bpcEl = document.getElementById('prog-bpc');
    if (!loopLenEl || !chordsEl || !bpcEl) return;
    const loopBars = parseFloat(loopLenEl.value) || 8;
    const chordsStr = chordsEl.value.trim();
    const nChords = chordsStr ? chordsStr.split(/\s+/).length : 0;
    if (nChords === 0) {
      bpcEl.value = '—';
      return;
    }
    const computedBpc = loopBars / nChords;
    bpcEl.value = computedBpc;
  }
  function updateProgLengthReadout() {
    const readout = document.getElementById('prog-length-text');
    const wrapper = document.getElementById('prog-length-readout');
    if (!readout || !wrapper) return;
    // First recompute bpc from loop length
    recomputeBpcFromLoopLength();
    const chordsStr = document.getElementById('prog-chords').value.trim();
    const nChords = chordsStr ? chordsStr.split(/\s+/).length : 0;
    const bpm = parseFloat(document.getElementById('prog-bpm').value) || 120;
    const bpc = parseFloat(document.getElementById('prog-bpc').value) || 2;
    if (nChords === 0) {
      readout.textContent = '(no chords entered)';
      wrapper.style.borderColor = 'var(--border)';
      return;
    }
    const totalBars = nChords * bpc;
    const totalBeats = totalBars * 4;
    const secPerChord = (60.0 / bpm) * 4 * bpc;
    const totalSec = secPerChord * nChords;
    const isPow2 = totalBars > 0 && Number.isInteger(totalBars) && (totalBars & (totalBars - 1)) === 0;
    // bpc must be a clean whole/half number for the loop to land on grid
    const bpcClean = (bpc * 2) % 1 === 0;  // allows 0.5, 1, 1.5, 2, 2.5, 3, etc.
    const looksGood = isPow2 && bpcClean;
    let status;
    if (looksGood && Number.isInteger(bpc)) {
      status = '✓ perfect loop';
    } else if (!bpcClean) {
      status = `⚠ ${nChords} chords doesn't divide cleanly into ${totalBars} bars — try ${nChords*1}/${nChords*2}/${nChords*4} bars`;
    } else if (!isPow2) {
      status = '⚠ NOT a power-of-2 bar count — will drift against click';
    } else {
      status = '◐ half-bar changes (still loops, but unusual)';
    }
    readout.textContent = `${nChords} chords × ${bpc} bars = ${totalBars} bars / ${totalBeats} beats @ ${bpm} BPM  (${secPerChord.toFixed(1)}s per chord, ${totalSec.toFixed(1)}s total)  ${status}`;
    // Color: green for perfect, amber for half-bar, red for not-a-loop
    if (looksGood && Number.isInteger(bpc)) {
      wrapper.style.borderColor = 'var(--border)';
      wrapper.style.background = 'var(--bg-3)';
      readout.style.color = 'var(--fg-dim)';
    } else if (!bpcClean || !isPow2) {
      wrapper.style.borderColor = '#c44';
      wrapper.style.background = 'rgba(196, 68, 68, 0.08)';
      readout.style.color = '#e88';
    } else {
      wrapper.style.borderColor = 'var(--amber)';
      wrapper.style.background = 'rgba(242, 183, 5, 0.06)';
      readout.style.color = 'var(--amber)';
    }
  }
  // Wire inputs that affect loop length. Loop length dropdown drives bpc.
  ['prog-chords', 'prog-bpm', 'prog-loop-length'].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('input', updateProgLengthReadout);
      el.addEventListener('change', updateProgLengthReadout);
    }
  });
  // initial render
  setTimeout(updateProgLengthReadout, 100);

  // ---------- system audio output switcher ----------
  // Lists macOS output devices and lets you switch the default with one click.
  // Requires switchaudio-osx (`brew install switchaudio-osx`); shows install hint if missing.
  async function loadAudioOutputs() {
    const sel = document.getElementById('audio-output-select');
    const current = document.getElementById('audio-output-current');
    const hint = document.getElementById('audio-output-hint');
    if (!sel || !current) return;
    try {
      const r = await fetch('/api/audio-output/list');
      const data = await r.json();
      const devices = data.devices || [];
      const currentDev = devices.find(d => d.is_current);
      if (data.switcher_available) {
        // switchaudio-osx is installed — show the real dropdown for one-click switch
        sel.style.display = 'block';
        current.style.display = 'none';
        sel.innerHTML = '';
        devices.forEach(d => {
          const opt = document.createElement('option');
          opt.value = d.name;
          opt.textContent = d.name + (d.is_current ? '  ← current' : '');
          if (d.is_current) opt.selected = true;
          sel.appendChild(opt);
        });
        sel.disabled = false;
        hint.textContent = `${devices.length} outputs · click to switch`;
        hint.style.color = 'var(--fg-faint)';
      } else {
        // No switchaudio-osx — read-only display. The "⚙ change" button opens System Settings.
        sel.style.display = 'none';
        current.style.display = 'block';
        current.textContent = currentDev ? currentDev.name : '(no output detected)';
        hint.innerHTML = `${devices.length} outputs available · click <b>⚙ change</b> to switch in System Settings · (<code style="background: var(--bg-3); padding: 1px 4px; border-radius: 2px;">brew install switchaudio-osx</code> for one-click switching)`;
        hint.style.color = 'var(--fg-faint)';
      }
    } catch (e) {
      hint.textContent = `error: ${e.message}`;
      hint.style.color = 'var(--red, #c44)';
    }
  }
  // "⚙ change" button — always opens System Settings → Sound, works regardless of switchaudio-osx
  document.getElementById('audio-output-settings-btn')?.addEventListener('click', async () => {
    try {
      await fetch('/api/audio-output/open-settings', { method: 'POST' });
      setStatus('🎧 Opened System Settings → Sound. Pick your output device there.', '');
    } catch (e) {
      setStatus(`Couldn't open System Settings: ${e.message}`, 'error');
    }
  });
  document.getElementById('audio-output-select')?.addEventListener('change', async (e) => {
    const name = e.target.value;
    const hint = document.getElementById('audio-output-hint');
    if (!name) return;
    hint.textContent = `switching to ${name}…`;
    try {
      const r = await fetch('/api/audio-output/set', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      const d = await r.json();
      if (d.ok) {
        setStatus(`🎧 Output switched to ${name}`, 'success');
        setTimeout(loadAudioOutputs, 300);
      } else if (d.error && d.error.includes('switchaudio-osx')) {
        // Fallback path — open macOS Sound prefs so user can switch manually
        await fetch('/api/audio-output/open-settings', { method: 'POST' });
        setStatus(`🎧 Opened System Settings → Sound. Pick "${name}" in the Output tab.`, '');
        hint.textContent = `→ pick ${name} in System Settings → Sound → Output`;
      } else {
        setStatus(`Switch failed: ${d.error}`, 'error');
        hint.textContent = d.error;
        hint.style.color = 'var(--red, #c44)';
      }
    } catch (e) {
      setStatus(`Switch error: ${e.message}`, 'error');
    }
  });
  // Audio output switcher REMOVED 2026-05-26 — wedged USB-MIDI when toggled.
  // No polling, no init. Endpoints remain server-side but UI is gone.
  // loadAudioOutputs();
  // setInterval(loadAudioOutputs, 10000);

  // ---------- monitor toggle (input passthrough to system output) ----------
  // Routes TX-6 input → Mac's default output device (W+, speakers, AirPods).
  // Auto-mutes during captures so the live monitor doesn't bleed into the recording.
  // Server polls own state; UI polls /api/monitor/status every 3s for live "muted" feedback.
  function setMonitorUI(state) {
    const led = document.getElementById('monitor-led');
    const btn = document.getElementById('monitor-toggle-btn');
    const status = document.getElementById('monitor-status');
    if (!led || !btn) return;
    if (state.running) {
      if (state.muted_by_capture) {
        led.style.background = 'var(--amber)';
        led.style.boxShadow = '0 0 6px rgba(242, 183, 5, 0.6)';
        if (status) status.textContent = '🔇 muted — capturing';
      } else {
        led.style.background = '#5fb866';
        led.style.boxShadow = '0 0 6px rgba(95, 184, 102, 0.6)';
        if (status) status.textContent = `live → ${state.input_device || 'system out'}`;
      }
      btn.textContent = 'STOP';
      btn.classList.remove('secondary');
      btn.classList.add('red');
    } else {
      led.style.background = '#444';
      led.style.boxShadow = 'none';
      btn.textContent = 'ON';
      btn.classList.remove('red');
      btn.classList.add('secondary');
      if (status) status.textContent = state.last_error ? `⚠ ${state.last_error}` : '';
    }
  }
  async function fetchMonitorStatus() {
    try {
      const r = await fetch('/api/monitor/status');
      const s = await r.json();
      setMonitorUI(s);
      return s;
    } catch (e) { return null; }
  }
  document.getElementById('monitor-toggle-btn')?.addEventListener('click', async () => {
    const led = document.getElementById('monitor-led');
    const isRunning = led.style.background.includes('95') || led.style.background.includes('amber');
    if (isRunning) {
      await fetch('/api/monitor/stop', { method: 'POST' });
      await fetchMonitorStatus();
      setStatus('Monitor stopped.', '');
    } else {
      const settings = getAudioSettings();
      const r = await fetch('/api/monitor/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          audio_device: settings.audio_device,
          input_channels: settings.input_channels,
        }),
      });
      const d = await r.json();
      if (d.ok) {
        await fetchMonitorStatus();
        setStatus(`🔊 Monitor on — ${settings.audio_device} → system out`, 'success');
      } else {
        setStatus(`Monitor failed: ${d.error || 'unknown'}`, 'error');
      }
    }
  });
  // poll for "muted_by_capture" updates so the LED reflects capture state live
  setInterval(fetchMonitorStatus, 3000);
  fetchMonitorStatus();

  // ---------- persistent MIDI clock toggle ----------
  function setClockUI(running, bpm) {
    const led = document.getElementById('clock-led');
    const btn = document.getElementById('clock-toggle-btn');
    const bpmInput = document.getElementById('clock-bpm');
    if (running) {
      led.style.background = '#5fb866';
      led.style.boxShadow = '0 0 6px rgba(95, 184, 102, 0.6)';
      btn.textContent = 'STOP';
      btn.classList.remove('secondary');
      btn.classList.add('red');
      if (bpm) bpmInput.value = bpm;
    } else {
      led.style.background = '#444';
      led.style.boxShadow = 'none';
      btn.textContent = 'START';
      btn.classList.remove('red');
      btn.classList.add('secondary');
    }
  }
  async function fetchClockStatus() {
    try {
      const r = await fetch('/api/clock/status');
      const s = await r.json();
      setClockUI(s.running, s.bpm);
      return s;
    } catch (e) { return null; }
  }
  // Auto-start (or re-sync) the persistent MIDI clock to the current synth + BPM.
  // Idempotent on the server side — no-op if params already match.
  async function autoStartClock() {
    const bpm = parseFloat(document.getElementById('clock-bpm').value) || 120;
    const settings = getAudioSettings();
    if (!settings.midi_port) return;  // can't lock without a MIDI port
    try {
      const r = await fetch('/api/clock/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bpm, midi_port: settings.midi_port }),
      });
      const d = await r.json();
      if (d.ok) {
        setClockUI(true, bpm);
        if (!d.already_running) {
          setStatus(`🔒 Clock locked to ${settings.midi_port} @ ${bpm} BPM`, 'success');
        }
      } else {
        setClockUI(false);
        setStatus(`Clock failed: ${d.error || 'no MIDI port'}`, 'error');
      }
    } catch (e) {
      setStatus(`Clock error: ${e.message}`, 'error');
    }
  }
  // Toggle button is now an emergency-stop / re-lock, not a primary control.
  // Clock should always be on; user only flips this off in rare debugging cases.
  document.getElementById('clock-toggle-btn').addEventListener('click', async () => {
    const led = document.getElementById('clock-led');
    const isRunning = led.style.background.includes('95'); // green
    if (isRunning) {
      await fetch('/api/clock/stop', { method: 'POST' });
      setClockUI(false);
      setStatus('MIDI clock stopped (synth no longer locked).', '');
    } else {
      await autoStartClock();
    }
  });
  // BPM live-update — fires on every value change (drag, arrow, type) so tempo
  // updates in real time without waiting for blur/enter.
  // Uses the lightweight /api/clock/bpm endpoint that just updates state,
  // no MIDI Start re-trigger (so arp doesn't snap back to 1 on every tweak).
  let bpmPostDebounce = null;
  function postLiveBpm(bpm) {
    if (bpmPostDebounce) clearTimeout(bpmPostDebounce);
    bpmPostDebounce = setTimeout(async () => {
      try {
        await fetch('/api/clock/bpm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ bpm }),
        });
      } catch (e) {}
    }, 40);  // 40ms debounce — fast enough to feel live, slow enough to coalesce drags
  }
  document.getElementById('clock-bpm').addEventListener('input', () => {
    const bpm = parseFloat(document.getElementById('clock-bpm').value) || 120;
    // mirror to prog-bpm (two-way bound — see prog-bpm listener below)
    const progBpm = document.getElementById('prog-bpm');
    if (progBpm) progBpm.value = bpm;
    // mirror to drums-bpm if visible
    const drumsBpm = document.getElementById('drums-bpm');
    if (drumsBpm && !drumsBpm.value) drumsBpm.placeholder = `(pattern: ${bpm} BPM)`;
    postLiveBpm(bpm);
  });
  // When user changes the pattern's BPM field, mirror it back to the clock
  // so the click + synth follow what they typed next to the chord name.
  const progBpmEl = document.getElementById('prog-bpm');
  if (progBpmEl) {
    progBpmEl.addEventListener('input', () => {
      const bpm = parseFloat(progBpmEl.value) || 120;
      const clockBpm = document.getElementById('clock-bpm');
      clockBpm.value = bpm;
      postLiveBpm(bpm);
    });
  }
  // ⏮ 1 button — Ableton-style transport reset (MIDI Stop→Start, snaps synth arp to downbeat)
  document.getElementById('clock-reset-btn').addEventListener('click', async () => {
    const btn = document.getElementById('clock-reset-btn');
    btn.classList.add('flash');
    try {
      const r = await fetch('/api/clock/reset', { method: 'POST' });
      const d = await r.json();
      if (d.ok) {
        setStatus('⏮ Transport reset — synth locked to downbeat.', 'success');
        // also reset the browser click track so it counts from "1" too
        if (clickState && clickState.running) {
          clickState.tickCount = 0;
          clickState.nextTickTime = clickState.ctx.currentTime + 0.05;
        }
      } else {
        setStatus(`Reset failed: ${d.error || 'unknown'}`, 'error');
      }
    } catch (e) {
      setStatus(`Reset error: ${e.message}`, 'error');
    }
    setTimeout(() => btn.classList.remove('flash'), 200);
  });
  // MIDI port change → restart clock against the newly-selected synth
  document.getElementById('set-midi-port').addEventListener('change', () => {
    autoStartClock();
  });
  // On page load: poll once, then auto-start if not running (give settings a moment to populate)
  fetchClockStatus();
  setTimeout(() => { autoStartClock(); }, 1500);  // wait for instrument settings to load first
  setInterval(fetchClockStatus, 30000);  // poll every 30 sec to catch external state changes

  // ---------- browser-side click track (does NOT route through TX-6, won't bleed into captures) ----------
  const clickState = {
    running: false,
    ctx: null,
    gain: null,
    schedulerId: null,
    nextTickTime: 0,
    tickCount: 0,
  };
  function setClickUI(running) {
    const led = document.getElementById('click-led');
    const btn = document.getElementById('click-toggle-btn');
    if (running) {
      led.style.background = '#5fb866';
      led.style.boxShadow = '0 0 6px rgba(95, 184, 102, 0.6)';
      btn.textContent = 'OFF';
      btn.classList.remove('secondary');
      btn.classList.add('red');
    } else {
      led.style.background = '#444';
      led.style.boxShadow = 'none';
      btn.textContent = 'ON';
      btn.classList.remove('red');
      btn.classList.add('secondary');
    }
  }
  function scheduleClick(time, isAccent) {
    // Gentle metronome — sine wave, lower freqs, soft envelope.
    // Sounds like a wood-block tick rather than a piercing click.
    const ctx = clickState.ctx;
    const osc = ctx.createOscillator();
    const env = ctx.createGain();
    osc.type = 'sine';                                 // sine = no harsh harmonics
    osc.frequency.value = isAccent ? 900 : 600;        // lower = warmer, less piercing
    // soft attack, gentle decay — "tonk" not "tick"
    env.gain.setValueAtTime(0, time);
    env.gain.linearRampToValueAtTime(isAccent ? 0.5 : 0.3, time + 0.003);
    env.gain.exponentialRampToValueAtTime(0.0001, time + 0.06);
    osc.connect(env);
    env.connect(clickState.gain);
    osc.start(time);
    osc.stop(time + 0.08);
  }
  function clickScheduler() {
    if (!clickState.running) return;
    const ctx = clickState.ctx;
    const bpm = parseFloat(document.getElementById('clock-bpm').value) || 120;
    const subdiv = parseInt(document.getElementById('click-subdiv').value) || 4;
    // interval between subdivisions in seconds
    const interval = 60.0 / bpm / subdiv;
    // schedule any clicks falling inside the next ~120ms lookahead window
    while (clickState.nextTickTime < ctx.currentTime + 0.12) {
      // accent on the downbeat (every Nth click where N = subdiv multiplier)
      const isAccent = (clickState.tickCount % subdiv) === 0;
      scheduleClick(clickState.nextTickTime, isAccent);
      clickState.nextTickTime += interval;
      clickState.tickCount += 1;
    }
    clickState.schedulerId = setTimeout(clickScheduler, 25);
  }
  function startClick() {
    if (clickState.running) return;
    if (!clickState.ctx) {
      clickState.ctx = new (window.AudioContext || window.webkitAudioContext)();
      clickState.gain = clickState.ctx.createGain();
      clickState.gain.connect(clickState.ctx.destination);
    }
    // apply current volume
    const vol = parseInt(document.getElementById('click-vol').value) / 100;
    clickState.gain.gain.value = vol;
    clickState.running = true;
    clickState.tickCount = 0;
    clickState.nextTickTime = clickState.ctx.currentTime + 0.05;
    setClickUI(true);
    clickScheduler();
  }
  function stopClick() {
    clickState.running = false;
    if (clickState.schedulerId) {
      clearTimeout(clickState.schedulerId);
      clickState.schedulerId = null;
    }
    setClickUI(false);
  }
  document.getElementById('click-toggle-btn').addEventListener('click', () => {
    if (clickState.running) stopClick();
    else startClick();
  });
  // live volume control
  document.getElementById('click-vol').addEventListener('input', (e) => {
    if (clickState.gain) {
      clickState.gain.gain.value = parseInt(e.target.value) / 100;
    }
  });
  // Recalibrate the click: schedule the next tick at the CURRENT moment using
  // the CURRENT BPM. Reset tick counter so next click is an accent.
  // Called when BPM or subdivision changes so the click instantly follows the new tempo
  // (otherwise we'd wait up to a full beat at the old rate before the change took effect).
  function recalibrateClick() {
    if (!clickState.running || !clickState.ctx) return;
    clickState.nextTickTime = clickState.ctx.currentTime + 0.05;
    clickState.tickCount = 0;
  }
  // subdivision change → recalibrate so the new rate kicks in immediately
  document.getElementById('click-subdiv').addEventListener('change', recalibrateClick);
  // BPM change → recalibrate click so it follows the new tempo immediately.
  // Listen on both clock-bpm and prog-bpm since they're two-way bound.
  document.getElementById('clock-bpm').addEventListener('input', recalibrateClick);
  const _progBpmForClick = document.getElementById('prog-bpm');
  if (_progBpmForClick) _progBpmForClick.addEventListener('input', recalibrateClick);

  // ---------- MIDI panic ----------
  document.getElementById('panic-btn').addEventListener('click', async () => {
    const settings = getAudioSettings();
    setStatus(`Sending MIDI panic to ${settings.midi_port}…`, 'busy');
    try {
      const r = await fetch('/api/midi/panic', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ midi_port: settings.midi_port }),
      });
      const data = await r.json();
      if (data.ok) {
        setStatus(`⚡ Panic sent — All Notes Off + All Sound Off on all 16 channels (${data.port})`, 'success');
      } else {
        setStatus(`Panic failed: ${data.error}`, 'error');
      }
    } catch (e) {
      setStatus(`Panic error: ${e.message}`, 'error');
    }
  });

  // ---------- build pack ----------
  document.getElementById('build-pack-btn').addEventListener('click', async () => {
    const btn = document.getElementById('build-pack-btn');
    const status = document.getElementById('build-status');
    btn.disabled = true;
    status.textContent = 'Building pack... this takes ~60 sec.';
    status.style.color = 'var(--amber)';
    try {
      const r = await fetch('/api/build-pack', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instrument: els.instrumentSelect.value, vol: 1 }),
      });
      const data = await r.json();
      if (data.ok) {
        status.textContent = '✓ Pack built. Check releases/ folder.';
        status.style.color = 'var(--green)';
        // Show last bit of stdout in console card
        els.consoleCard.style.display = 'block';
        els.consoleEl.textContent = data.stdout_tail || '';
      } else {
        status.textContent = `Build failed: ${data.error || 'see console'}`;
        status.style.color = 'var(--red)';
        els.consoleCard.style.display = 'block';
        els.consoleEl.textContent = data.stderr || data.stdout || JSON.stringify(data);
      }
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
      status.style.color = 'var(--red)';
    } finally {
      btn.disabled = false;
    }
  });

  // ---------- drum patterns ----------
  async function loadDrumPatterns() {
    try {
      const r = await fetch('/api/patterns/drums');
      const patterns = await r.json();
      const sel = document.getElementById('drums-pattern');
      patterns.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.name;
        opt.dataset.bpm = p.bpm || 120;
        opt.dataset.vibe = p.vibe || '';
        const tags = (p.tags || []).slice(0, 2).join(' · ');
        opt.textContent = `${p.name}  ·  ${p.bpm}bpm  ${tags ? '· ' + tags : ''}`;
        sel.appendChild(opt);
      });
      sel.addEventListener('change', () => {
        const opt = sel.options[sel.selectedIndex];
        document.getElementById('drums-pattern-vibe').textContent = opt.dataset.vibe || '';
        if (opt.dataset.bpm) document.getElementById('drums-bpm').placeholder = `(pattern: ${opt.dataset.bpm} BPM)`;
      });
    } catch (e) {
      console.error('drum patterns load failed', e);
    }
  }

  // ---------- multisample quick presets ----------
  const MS_PRESETS = {
    bass: { range: 'a1-a4', step: 3, vel: 100, sustain: 4 },
    sub:  { range: 'c2-c5', step: 3, vel: 100, sustain: 4 },
    lead: { range: 'a2-a5', step: 3, vel: 100, sustain: 4 },
    pad:  { range: 'c2-c5', step: 3, vel: 100, sustain: 6 },
    stab: { range: 'a2-a5', step: 3, vel: 100, sustain: 2 },
  };
  document.querySelectorAll('[data-ms-preset]').forEach(btn => {
    btn.addEventListener('click', () => {
      const preset = MS_PRESETS[btn.dataset.msPreset];
      if (!preset) return;
      document.getElementById('ms-range').value = preset.range;
      document.getElementById('ms-step').value = preset.step;
      document.getElementById('ms-vel').value = preset.vel;
      document.getElementById('ms-sustain').value = preset.sustain;
      // visual feedback
      btn.style.background = 'var(--amber)';
      btn.style.color = '#000';
      setTimeout(() => { btn.style.background = ''; btn.style.color = ''; }, 300);
    });
  });

  // ---------- scan ALL audio devices (finds where signal is) ----------
  document.getElementById('scan-all-btn').addEventListener('click', async () => {
    const settings = getAudioSettings();
    setStatus(`Scanning every audio device on the Mac — sending MIDI + recording each (~30 sec)…`, 'busy');
    try {
      const r = await fetch('/api/scan-all-devices', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ midi_port: settings.midi_port, note: 45 }),
      });
      const data = await r.json();
      if (!data.ok) {
        setStatus(`Multi-device scan failed: ${data.error}`, 'error');
        return;
      }
      const results = data.results || [];
      const best = data.best_device;
      // Show full results in console card
      els.consoleCard.style.display = 'block';
      let html = '=== MULTI-DEVICE SCAN ===\n\n';
      results.forEach(r => {
        if (r.error) {
          html += `❌ ${r.device}: ${r.error}\n`;
        } else if (r.hot_channels.length > 0) {
          html += `🔥 ${r.device} (#${r.index}, ${r.channels}ch @ ${r.sample_rate}Hz)\n`;
          html += `    Signal on channels: ${r.hot_channels.join(', ')}\n`;
          html += `    Max peak: ${r.max_peak_db?.toFixed(1)} dBFS\n`;
        } else {
          html += `⚫ ${r.device} (#${r.index}, ${r.channels}ch) — silent\n`;
        }
      });
      els.consoleEl.textContent = html;
      if (best) {
        setStatus(`✓ FOUND IT: signal is on "${best.device}" — channels ${best.hot_channels.join(',')}. Switch your audio device to that.`, 'success');
        // optionally auto-select it
        const audioSel = document.getElementById('set-audio-device');
        if (Array.from(audioSel.options).some(o => o.value === best.device)) {
          audioSel.value = best.device;
          audioSel.dispatchEvent(new Event('change'));
          document.getElementById('set-input-channels').value = best.hot_channels.join(',');
          saveInstrumentSettings();
        }
      } else {
        setStatus(`⚠ NO audio device has signal anywhere. MIDI may be working but no recording-capable device is hearing the synth. Check macOS Sound input + TX-6 mode (USB Audio Class).`, 'error');
      }
    } catch (e) {
      setStatus(`Multi-scan error: ${e.message}`, 'error');
    }
  });

  // ---------- test MIDI (sends one note, no recording) ----------
  document.getElementById('test-midi-btn').addEventListener('click', async () => {
    const settings = getAudioSettings();
    setStatus(`Sending one MIDI note to ${settings.midi_port}…`, 'busy');
    try {
      const r = await fetch('/api/test-midi', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ midi_port: settings.midi_port, note: 60, duration: 0.5 }),
      });
      const data = await r.json();
      if (data.ok) {
        setStatus(`🔊 Sent note 60 (C4) to ${data.port} — should hear it on the synth. If you don't hear anything, MIDI routing is the issue. If you hear it but Check Level still fails, it's an AUDIO routing issue (TX-6 channel → USB).`, 'success');
      } else {
        setStatus(`MIDI test failed: ${data.error}`, 'error');
      }
    } catch (e) {
      setStatus(`Error: ${e.message}`, 'error');
    }
  });

  // ---------- preview chord progression in browser (Web Audio) ----------
  // Note name → MIDI number → frequency
  const NOTE_TO_SEMITONE = { 'c':0,'c#':1,'db':1,'d':2,'d#':3,'eb':3,'e':4,'f':5,'f#':6,'gb':6,'g':7,'g#':8,'ab':8,'a':9,'a#':10,'bb':10,'b':11 };
  function chordToMidi(name, baseOctave = 3) {
    // Parse "Cm" / "Cmaj7" / "Bb7" etc. into MIDI notes
    const m = name.match(/^([A-Ga-g])([#b]?)\s*(.*)$/);
    if (!m) return null;
    const [, letter, acc, suffix] = m;
    const rootKey = (letter + acc).toLowerCase();
    let rootSt = NOTE_TO_SEMITONE[rootKey];
    if (rootSt === undefined) rootSt = NOTE_TO_SEMITONE[letter.toLowerCase()] + (acc === '#' ? 1 : acc === 'b' ? -1 : 0);
    const intervals = {
      '': [0,4,7], 'maj':[0,4,7], 'major':[0,4,7], 'M':[0,4,7],
      'maj7':[0,4,7,11], 'M7':[0,4,7,11], 'maj9':[0,4,7,11,14],
      'm':[0,3,7], 'min':[0,3,7], 'minor':[0,3,7],
      'm7':[0,3,7,10], 'min7':[0,3,7,10], 'm9':[0,3,7,10,14],
      'm7b5':[0,3,6,10],
      '7':[0,4,7,10], 'dim':[0,3,6], 'dim7':[0,3,6,9], 'aug':[0,4,8],
      'sus2':[0,2,7], 'sus4':[0,5,7],
    }[suffix.trim()] || [0,4,7];
    const rootMidi = rootSt + (baseOctave + 1) * 12;
    return intervals.map(i => rootMidi + i);
  }
  const midiToFreq = (midi) => 440 * Math.pow(2, (midi - 69) / 12);

  let previewState = { ac: null, timeouts: [], mode: null, abort: false };

  function stopPreview() {
    previewState.abort = true;
    previewState.timeouts.forEach(clearTimeout);
    previewState.timeouts = [];
    if (previewState.ac) {
      try { previewState.ac.close(); } catch (e) {}
      previewState.ac = null;
    }
    // Also tell the backend to stop any live audition
    fetch('/api/audition-stop', { method: 'POST' }).catch(() => {});
    document.getElementById('preview-stop-btn').disabled = true;
    document.getElementById('preview-btn').disabled = false;
    document.getElementById('audition-btn').disabled = false;
    setStatus('Preview stopped.', '');
  }

  async function playChordProgression(chords, bpm, barsPerChord, sendMode) {
    stopPreview();
    previewState.abort = false;
    previewState.mode = 'browser';
    const ac = new (window.AudioContext || window.webkitAudioContext)();
    previewState.ac = ac;
    document.getElementById('preview-btn').disabled = true;
    document.getElementById('audition-btn').disabled = true;
    document.getElementById('preview-stop-btn').disabled = false;

    const secPerBar = (60.0 / bpm) * 4;
    const secPerChord = secPerBar * barsPerChord;
    const totalSec = secPerChord * chords.length;
    const loop = document.getElementById('preview-loop').checked;

    const masterGain = ac.createGain();
    masterGain.gain.value = 0.4;
    const filter = ac.createBiquadFilter();
    filter.type = 'lowpass';
    filter.frequency.value = 1800;
    filter.Q.value = 0.7;
    masterGain.connect(filter).connect(ac.destination);

    function schedulePass(startT) {
      let t = startT;
      for (let i = 0; i < chords.length; i++) {
        let notes = chordToMidi(chords[i], 3);
        if (!notes) continue;
        if (sendMode === 'root') notes = [notes[0]];
        if (sendMode === 'arp') {
          const arpInterval = secPerBar / 8 / 2;
          const arpNotes = Math.floor(secPerChord / arpInterval);
          for (let k = 0; k < arpNotes; k++) {
            const noteMidi = notes[k % notes.length];
            scheduleNote(ac, masterGain, t + k * arpInterval, midiToFreq(noteMidi), arpInterval * 0.7);
          }
        } else {
          for (const noteMidi of notes) {
            scheduleNote(ac, masterGain, t, midiToFreq(noteMidi), secPerChord * 0.95);
          }
        }
        t += secPerChord;
      }
      return t;
    }

    let nextStart = ac.currentTime + 0.05;
    if (loop) {
      const scheduleLoop = () => {
        if (previewState.abort) return;
        nextStart = schedulePass(nextStart);
        // schedule the next pass ~200ms before this one ends
        const delay = (nextStart - ac.currentTime - 0.2) * 1000;
        previewState.timeouts.push(setTimeout(scheduleLoop, Math.max(50, delay)));
      };
      scheduleLoop();
      setStatus(`▶ Looping in browser — ${chords.join(' ')} @ ${bpm} BPM (${sendMode} mode)`, 'success');
    } else {
      schedulePass(nextStart);
      previewState.timeouts.push(setTimeout(stopPreview, (totalSec + 0.5) * 1000));
      setStatus(`▶ Preview (browser) — ${chords.join(' ')} (one-shot)`, 'success');
    }
  }

  function scheduleNote(ac, dest, startTime, freq, duration) {
    const osc = ac.createOscillator();
    osc.type = 'sawtooth';
    osc.frequency.value = freq;
    const gain = ac.createGain();
    // ADSR envelope
    gain.gain.setValueAtTime(0, startTime);
    gain.gain.linearRampToValueAtTime(0.25, startTime + 0.01);  // attack
    gain.gain.exponentialRampToValueAtTime(0.15, startTime + 0.1);  // decay to sustain
    gain.gain.setValueAtTime(0.15, startTime + duration - 0.1);
    gain.gain.exponentialRampToValueAtTime(0.001, startTime + duration);  // release
    osc.connect(gain).connect(dest);
    osc.start(startTime);
    osc.stop(startTime + duration + 0.05);
  }

  document.getElementById('preview-btn').addEventListener('click', () => {
    const chordStr = document.getElementById('prog-chords').value.trim();
    if (!chordStr) return;
    const chords = chordStr.split(/\s+/);
    const bpm = parseFloat(document.getElementById('prog-bpm').value) || 120;
    const bpc = parseFloat(document.getElementById('prog-bpc').value) || 2;
    const sendMode = document.getElementById('prog-send-mode')?.value || 'chord';
    playChordProgression(chords, bpm, bpc, sendMode);
  });
  document.getElementById('preview-stop-btn').addEventListener('click', stopPreview);

  // 🎹 Live audition through the synth (loops MIDI to actual gear, no recording)
  // LOCK-TO-1: resets the browser click, MIDI clock transport, AND audition all
  // at the same moment so the progression's chord 1 lands on the click's "1".
  document.getElementById('audition-btn').addEventListener('click', async () => {
    const chordStr = document.getElementById('prog-chords').value.trim();
    if (!chordStr) return;
    const settings = getAudioSettings();
    const bpm = parseFloat(document.getElementById('prog-bpm').value) || 120;
    const bpc = parseFloat(document.getElementById('prog-bpc').value) || 2;
    const sendMode = document.getElementById('prog-send-mode')?.value || 'chord';
    stopPreview();
    previewState.mode = 'live';
    document.getElementById('preview-btn').disabled = true;
    document.getElementById('audition-btn').disabled = true;
    document.getElementById('preview-stop-btn').disabled = false;

    // ---- LOCK-TO-1 sequence ----
    // 1. Reset browser click: next tick will be an accent at "now+50ms" so click's "1"
    //    lines up with the moment everything else also resets to 1.
    if (clickState.running && clickState.ctx) {
      clickState.tickCount = 0;
      clickState.nextTickTime = clickState.ctx.currentTime + 0.05;
    }
    // 2. Reset MIDI clock transport (Stop → Start) so synth's internal arp/seq snaps to 1
    try {
      await fetch('/api/clock/reset', { method: 'POST' });
    } catch (e) { /* clock might not be running — ignore */ }
    // 3. Tiny pause so the synth processes the transport reset before chord 1 fires
    await new Promise(r => setTimeout(r, 30));
    // 4. Start the audition — its t_loop_start = now, which is "1" for everything

    try {
      const r = await fetch('/api/audition-start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          progression: chordStr, bpm, bars_per_chord: bpc, send_mode: sendMode,
          midi_port: settings.midi_port, midi_channel: 1,
        }),
      });
      const data = await r.json();
      if (data.ok) {
        setStatus(`🔒 Locked to 1 — auditioning ${chordStr} @ ${bpm} BPM through ${settings.midi_port}.`, 'success');
        startAuditionKeepalive();
      } else {
        setStatus(`Audition failed: ${data.error}`, 'error');
        document.getElementById('preview-btn').disabled = false;
        document.getElementById('audition-btn').disabled = false;
        document.getElementById('preview-stop-btn').disabled = true;
      }
    } catch (e) {
      setStatus(`Audition error: ${e.message}`, 'error');
    }
  });

  // ---------- audition safety: keepalive + tab-close auto-stop ----------
  // Without this, closing the browser while audition is running leaves the synth
  // looping notes forever. Two layers of defense:
  //   1. While audition is running, ping the server every 15s. Server auto-stops
  //      if pings stop for 60s (4 misses) — covers crashed tab / network loss.
  //   2. On tab close, fire a stop beacon. sendBeacon survives unload reliably,
  //      unlike fetch which can be cancelled.
  let auditionKeepaliveId = null;
  function startAuditionKeepalive() {
    if (auditionKeepaliveId) clearInterval(auditionKeepaliveId);
    // ping immediately, then every 15s
    fetch('/api/audition-ping', { method: 'POST' }).catch(() => {});
    auditionKeepaliveId = setInterval(() => {
      fetch('/api/audition-ping', { method: 'POST' }).catch(() => {});
    }, 15000);
  }
  function stopAuditionKeepalive() {
    if (auditionKeepaliveId) {
      clearInterval(auditionKeepaliveId);
      auditionKeepaliveId = null;
    }
  }
  // On tab close — fire a stop beacon. sendBeacon is the right tool here because
  // browsers will let it complete even as the page unloads (regular fetch may not).
  window.addEventListener('beforeunload', () => {
    try {
      const blob = new Blob([''], { type: 'application/json' });
      navigator.sendBeacon('/api/audition-stop', blob);
      // Also send MIDI panic so any held note releases cleanly
      navigator.sendBeacon('/api/midi/panic', blob);
    } catch (e) { /* unload — nothing we can do */ }
  });
  // Hook the existing preview-stop button to also kill the keepalive
  const _stopBtn = document.getElementById('preview-stop-btn');
  if (_stopBtn) {
    _stopBtn.addEventListener('click', stopAuditionKeepalive);
  }

  // ---------- generate fresh chord progression (algorithmic) ----------
  document.getElementById('gen-btn').addEventListener('click', async () => {
    const genre = document.getElementById('gen-genre').value;
    const key = document.getElementById('gen-key').value;
    const out = document.getElementById('gen-output');
    out.textContent = 'Generating…';
    out.style.color = 'var(--amber)';
    try {
      const r = await fetch('/api/generate-progression', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ genre, key, seed: Date.now() % 100000 }),
      });
      const data = await r.json();
      if (data.ok) {
        const chordStr = data.chords.join(' ');
        // auto-fill the form
        document.getElementById('prog-name').value = `${genre}-${key.toLowerCase()}-gen`;
        document.getElementById('prog-chords').value = chordStr;
        out.style.color = 'var(--green)';
        out.innerHTML = `✓ <b style="color: var(--fg);">${chordStr}</b> &nbsp;·&nbsp; <span style="color: var(--fg-dim);">${data.template} in ${key} (${data.mode})</span>`;
      } else {
        out.style.color = 'var(--red)';
        out.textContent = `Generate failed: ${data.error}`;
      }
    } catch (e) {
      out.style.color = 'var(--red)';
      out.textContent = `Error: ${e.message}`;
    }
  });

  // ---------- channel scan (find which input the synth is on) ----------
  document.getElementById('scan-btn').addEventListener('click', async () => {
    const settings = getAudioSettings();
    setStatus(`Scanning ${settings.audio_device} channels — sending one note + listening on all channels (5 sec)…`, 'busy');
    try {
      const r = await fetch('/api/scan-channels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ audio_device: settings.audio_device, midi_port: settings.midi_port }),
      });
      const data = await r.json();
      if (!data.ok) {
        setStatus(`Scan failed: ${data.stdout_tail || data.error}`, 'error');
        return;
      }
      const active = data.active_channels || [];
      if (active.length === 0) {
        setStatus(`⚠ No channels have signal. Check: synth audio cable plugged in? TX-6 channel volume up?`, 'error');
      } else {
        const peakInfo = (data.per_channel || []).filter(c => c.db !== null).map(c => `ch${c.channel}: ${c.db.toFixed(1)}dB`).join(' · ');
        const currentRaw = document.getElementById('set-input-channels').value.trim();
        const currentChs = currentRaw.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n));
        const currentOk = currentChs.length > 0 && currentChs.every(c => active.includes(c));
        if (currentOk) {
          setStatus(`✓ Signal on ch ${active.join(',')} (${peakInfo}). Your current setting "${currentRaw}" is hot ✓`, 'success');
        } else {
          // Report what's hot, but DON'T overwrite the user's setting
          setStatus(`ℹ Signal found on ch ${active.join(',')} (${peakInfo}). Your current setting "${currentRaw}" isn't in the hot list — adjust the dropdown manually if you want.`, 'success');
        }
      }
    } catch (e) {
      setStatus(`Scan error: ${e.message}`, 'error');
    }
  });

  // ---------- meter (level check) ----------
  document.getElementById('meter-btn').addEventListener('click', async () => {
    const settings = getAudioSettings();
    setStatus(`Checking level on ${settings.audio_device} ch ${settings.input_channels} — play a loud note NOW (5 sec)…`, 'busy');
    try {
      const r = await fetch('/api/meter', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ duration: 5, ...settings }),
      });
      const data = await r.json();
      const isSilent = data.ok && (data.peak_db === null || data.peak_db < -40);
      if (data.ok && data.peak_db !== null && data.peak_db >= -40) {
        const db = data.peak_db;
        let verdict;
        if (db > -3) verdict = `⚠ HOT — lower input gain (peak ${db.toFixed(1)} dBFS)`;
        else if (db < -18) verdict = `↑ quiet — could raise gain (peak ${db.toFixed(1)} dBFS)`;
        else verdict = `✓ peak ${db.toFixed(1)} dBFS — healthy headroom on ch ${settings.input_channels}`;
        setStatus(verdict, db > -3 ? 'error' : 'success');
      } else if (isSilent) {
        // Auto fall-back: scan all channels to see if the synth is on a different input
        setStatus(`⚠ Silent on ch ${settings.input_channels}. Auto-scanning all channels to find signal…`, 'busy');
        try {
          const sr = await fetch('/api/scan-channels', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ audio_device: settings.audio_device, midi_port: settings.midi_port }),
          });
          const sdata = await sr.json();
          const active = sdata.active_channels || [];
          if (active.length > 0) {
            const rec = sdata.recommendation;
            document.getElementById('set-input-channels').value = rec;
            saveInstrumentSettings();
            setStatus(`⚠ Channel ${settings.input_channels} was silent — but found signal on ${active.join(',')}. Auto-updated input channels to ${rec}. Try Check Level again.`, 'success');
          } else {
            setStatus(`⚠ NO channel has signal. Audio side broken. Try 🔊 Test MIDI to confirm MIDI works. If MIDI plays but no channel records, your TX-6 (or interface) isn't routing the synth's input to USB. Check the channel's USB-send routing on the device.`, 'error');
          }
        } catch (e2) {
          setStatus(`Silent + scan also failed: ${e2.message}`, 'error');
        }
      } else {
        setStatus(`Meter failed: ${data.stdout || data.error}`, 'error');
      }
    } catch (e) {
      setStatus(`Meter error: ${e.message}`, 'error');
    }
  });

  // ---------- session report ----------
  // Live tally + breakdown that sits above the captures list. Hidden when empty.
  function renderSessionReport(captures) {
    const report = document.getElementById('session-report');
    if (!report) return;
    if (!captures || captures.length === 0) {
      report.style.display = 'none';
      return;
    }
    report.style.display = 'block';
    // counts
    const total = captures.length;
    // Done = anything not tossed (no separate "keep" step anymore); nothing truly pends.
    const kept = captures.filter(c => c.status !== 'discarded').length;
    const pending = 0;
    const discarded = captures.filter(c => c.status === 'discarded').length;
    document.getElementById('sr-total').textContent = total;
    document.getElementById('sr-kept').textContent = kept;
    document.getElementById('sr-pending').textContent = pending;
    document.getElementById('sr-discarded').textContent = discarded;
    // elapsed since earliest capture
    const times = captures.map(c => c.captured_at).filter(Boolean).sort();
    if (times.length) {
      const earliest = new Date(times[0]);
      const mins = Math.floor((Date.now() - earliest.getTime()) / 60000);
      const hrs = Math.floor(mins / 60);
      const remMin = mins % 60;
      document.getElementById('sr-elapsed').textContent =
        hrs > 0 ? `${hrs}h ${remMin}m session` : `${mins}m session`;
    }
    // styles breakdown
    const styleCount = {};
    captures.forEach(c => { styleCount[c.style] = (styleCount[c.style] || 0) + 1; });
    const stylesText = Object.entries(styleCount)
      .sort((a,b) => b[1] - a[1])
      .map(([s, n]) => `<span style="color: var(--fg);">${n}</span> ${s}`)
      .join('  ·  ');
    document.getElementById('sr-styles').innerHTML = stylesText ? `styles: ${stylesText}` : '';
    // BPMs used (unique)
    const bpms = [...new Set(captures.map(c => c.params?.bpm).filter(b => b != null))].sort((a,b) => a - b);
    document.getElementById('sr-bpms').innerHTML = bpms.length
      ? `BPMs: <span style="color: var(--fg);">${bpms.join(' / ')}</span>`
      : '';
    // top patches by capture count (kept + pending — discarded don't count)
    const liveCaptures = captures.filter(c => c.status !== 'discarded');
    const patchCount = {};
    liveCaptures.forEach(c => {
      const patch = c.params?.patch || c.name || '?';
      patchCount[patch] = (patchCount[patch] || 0) + 1;
    });
    const topPatches = Object.entries(patchCount)
      .sort((a,b) => b[1] - a[1])
      .slice(0, 5)
      .map(([p, n]) => `${realName(p)} (${n})`)
      .join('  ·  ');
    document.getElementById('sr-top-patches').textContent = topPatches ? `top: ${topPatches}` : '';
  }

  // ---------- captures log ----------
  async function loadCapturesLog() {
    try {
      const r = await fetch('/api/captures?t=' + Date.now());  // cache-bust so a refresh always reflects reality (clears/deletes show immediately)
      const captures = await r.json();
      renderSessionReport(captures);
      const listEl = document.getElementById('captures-list');
      if (captures.length === 0) {
        listEl.innerHTML = '<div class="small" style="color: var(--fg-faint); padding: 8px;">No captures yet this session. Fire one above.</div>';
        return;
      }
      listEl.innerHTML = captures.slice(0, 15).map(c => {
        // No "keep" step anymore (capture + processing is automatic), so a logged capture that
        // wasn't tossed simply IS done — show it green ✓, not the old pending half-circle ◐.
        const statusColor = c.status === 'discarded' ? 'var(--fg-faint)' : 'var(--green)';
        const statusIcon  = c.status === 'discarded' ? '✗' : '✓';
        const time = c.captured_at?.slice(11, 19) || '';
        // Per-row inline actions for pending captures: audition + quick delete.
        // Audition + toss stay on every kept capture (not just "pending"), so a done patch can
        // still be previewed or removed — only a tossed one loses its actions.
        const actions = c.status !== 'discarded'
          ? `
            <audio controls preload="none" src="${c.wav_url}" style="width: 200px; height: 28px;"></audio>
            <button class="red row-discard-btn" data-wav="${c.wav_path}" style="padding: 4px 10px; font-size: 11px; margin: 0;" title="Toss this capture — moves to Trash, removes it from the list">✗</button>
          `
          : '';
        return `
          <div style="display: flex; align-items: center; gap: 10px; padding: 6px 10px; background: var(--bg-3); border-left: 2px solid ${statusColor};">
            <span style="color: ${statusColor}; font-weight: 700; min-width: 16px;">${statusIcon}</span>
            <div style="flex: 1; min-width: 0;">
              <div style="font-size: 12px; color: var(--fg);">${realName(c.params?.patch || c.name)} <span style="color: var(--fg-faint); font-size: 10px;">· ${c.style} · ${time}</span>${c.params?.preset ? ` <span style="color: var(--amber); font-size: 10px;" title="played on this preset">🎛 ${c.params.preset}</span>` : ''}</div>
              <div class="small" style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${c.wav_path}</div>
            </div>
            ${actions}
          </div>
        `;
      }).join('');
      // Wire the per-row delete buttons
      listEl.querySelectorAll('.row-discard-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          const wavPath = btn.dataset.wav;
          // No confirm — the x just tosses it. It goes to the Trash (recoverable), so a slip
          // is no big deal; the point of the x is a quick clean-up.
          btn.disabled = true;
          btn.textContent = '…';
          try {
            const resp = await fetch('/api/discard', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ wav_path: wavPath }),
            });
            const d = await resp.json();
            if (d.ok) {
              setStatus(`🗑 Tossed to Trash — ${wavPath}`, '');
              loadCapturesLog();  // row disappears immediately
            } else {
              setStatus(`Delete failed: ${d.error}`, 'error');
              btn.disabled = false;
              btn.textContent = '✗';
            }
          } catch (err) {
            setStatus(`Delete error: ${err.message}`, 'error');
            btn.disabled = false;
            btn.textContent = '✗';
          }
        });
      });
    } catch (e) {
      console.error('failed to load captures', e);
    }
  }

  // ---------- clear session captures log ----------
  document.getElementById('captures-clear-all-btn')?.addEventListener('click', async () => {
    if (!confirm('Clear the session captures log?\n\nThis only wipes this panel — WAVs on disk stay where they are.')) return;
    try {
      const r = await fetch('/api/captures/clear', { method: 'POST' });
      const d = await r.json();
      if (d.ok) {
        setStatus(`✗ Cleared ${d.cleared} entries from session log`, '');
        loadCapturesLog();  // refresh — should now be empty
      }
    } catch (e) {
      setStatus(`Clear failed: ${e.message}`, 'error');
    }
  });

  // ---------- zip pack ----------
  document.getElementById('zip-pack-btn').addEventListener('click', async () => {
    const btn = document.getElementById('zip-pack-btn');
    const status = document.getElementById('build-status');
    btn.disabled = true;
    status.textContent = 'Zipping pack…';
    status.style.color = 'var(--amber)';
    try {
      const r = await fetch('/api/zip-pack', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instrument: els.instrumentSelect.value, vol: 1 }),
      });
      const data = await r.json();
      if (data.ok) {
        status.textContent = `✓ Zipped: ${data.zip_path} (${data.size_mb} MB)`;
        status.style.color = 'var(--green)';
      } else {
        status.textContent = `Zip failed: ${data.error || 'see console'}`;
        status.style.color = 'var(--red)';
      }
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
      status.style.color = 'var(--red)';
    } finally {
      btn.disabled = false;
    }
  });

  // ---------- add instrument ----------
  document.getElementById('add-instrument-btn').addEventListener('click', () => {
    document.getElementById('add-instrument-form').style.display = 'block';
  });
  document.getElementById('cancel-instrument-btn').addEventListener('click', () => {
    document.getElementById('add-instrument-form').style.display = 'none';
  });
  document.getElementById('create-instrument-btn').addEventListener('click', async () => {
    const slug = document.getElementById('new-instr-slug').value.trim();
    const name = document.getElementById('new-instr-name').value.trim();
    const category = document.getElementById('new-instr-category').value;
    const tier = document.getElementById('new-instr-tier').value;
    if (!slug) { alert('Slug required'); return; }
    try {
      const r = await fetch('/api/instruments', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slug, name, category, tier }),
      });
      const data = await r.json();
      if (data.ok) {
        document.getElementById('add-instrument-form').style.display = 'none';
        document.getElementById('new-instr-slug').value = '';
        document.getElementById('new-instr-name').value = '';
        await loadInstruments();
        document.getElementById('instrument-select').value = data.slug;
        loadInstrument(data.slug);
      } else {
        alert(`Failed: ${data.error}`);
      }
    } catch (e) {
      alert(`Error: ${e.message}`);
    }
  });

  // ---------- pack audit ----------
  document.getElementById('audit-pack-btn').addEventListener('click', async () => {
    const btn = document.getElementById('audit-pack-btn');
    const resultsEl = document.getElementById('audit-results');
    btn.disabled = true;
    resultsEl.innerHTML = '<div class="small" style="color: var(--amber);">Auditing pack…</div>';
    try {
      const r = await fetch('/api/audit-pack', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instrument: els.instrumentSelect.value }),
      });
      const data = await r.json();
      if (data.error) {
        resultsEl.innerHTML = `<div class="status error">${data.error}</div>`;
        return;
      }
      const info = data.info || {};
      const statusColor = data.ok ? 'var(--green)' : 'var(--red)';
      const statusText = data.ok ? '✓ Pack is healthy' : `✗ ${data.issues.length} issue${data.issues.length === 1 ? '' : 's'}`;
      let html = `<div style="color: ${statusColor}; font-weight: 700; margin-bottom: 8px;">${statusText}</div>`;
      html += '<div style="font-size: 11px; line-height: 1.7;">';
      html += `<div>WAVs total: <b style="color: var(--fg);">${info.total_wavs || 0}</b></div>`;
      html += `<div>WAVs silent: <b style="color: ${info.silent_wavs?.length ? 'var(--red)' : 'var(--green)'};">${info.silent_wavs?.length || 0}</b></div>`;
      html += `<div>WAVs clipping: <b style="color: ${info.clipping_wavs?.length ? 'var(--red)' : 'var(--green)'};">${info.clipping_wavs?.length || 0}</b></div>`;
      html += `<div>SFZ presets: <b style="color: var(--green);">${info.sfz_ok || 0} ok</b>${info.sfz_issues?.length ? `, <b style="color: var(--red);">${info.sfz_issues.length} issues</b>` : ''}</div>`;
      html += `<div>Decent presets: <b style="color: var(--green);">${info.decent_ok || 0} ok</b>${info.decent_issues?.length ? `, <b style="color: var(--red);">${info.decent_issues.length} issues</b>` : ''}</div>`;
      html += `<div>Photos: <b style="color: var(--fg);">${info.photos_count || 0}</b></div>`;
      if (info.missing_required?.length) html += `<div style="color: var(--red);">Missing files: ${info.missing_required.join(', ')}</div>`;
      html += '</div>';
      if (data.issues?.length) {
        html += '<div style="margin-top: 8px; font-size: 11px; color: var(--red);">';
        data.issues.forEach(i => html += `<div>✗ ${i}</div>`);
        html += '</div>';
      }
      resultsEl.innerHTML = html;
    } catch (e) {
      resultsEl.innerHTML = `<div class="status error">${e.message}</div>`;
    } finally {
      btn.disabled = false;
    }
  });

  // ---------- open folder buttons ----------
  document.querySelectorAll('[data-open]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const kind = btn.dataset.open;
      try {
        await fetch('/api/open-folder', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind, instrument: els.instrumentSelect.value }),
        });
      } catch (e) {
        console.error('open folder failed', e);
      }
    });
  });

  // Refresh captures log after every capture/keep/discard action
  const origCaptureClick = els.captureBtn.onclick;
  els.captureBtn.addEventListener('click', () => { setTimeout(loadCapturesLog, 1000); setTimeout(loadCapturesLog, 5000); });
  // After scan, also save the auto-detected channels to this instrument's manifest
  const origScanAfter = async () => {
    await saveInstrumentSettings();
  };
  document.getElementById('scan-btn').addEventListener('click', () => setTimeout(origScanAfter, 6000));

  // ---------- composer (synthesis machine v0) ----------
  async function loadComposerGenres() {
    try {
      const r = await fetch('/api/compose/genres');
      const genres = await r.json();
      const sel = document.getElementById('compose-genre');
      if (!sel) return;
      sel.innerHTML = genres.map(g => `<option value="${g}">${g}</option>`).join('');
      // Default to house if available, else first
      if (genres.includes('house')) sel.value = 'house';
    } catch (e) { /* silent */ }
  }
  let composeSeed = null;
  let lastComposeResult = null;  // { bpm, tracks: [{midi_path, channel, label}] } — set when compose returns, audition reads
  async function runCompose() {
    const btn = document.getElementById('compose-btn');
    const resultEl = document.getElementById('compose-result');
    btn.disabled = true;
    btn.textContent = '⏳ Composing…';
    try {
      const body = {
        genre: document.getElementById('compose-genre').value,
        bpm: parseFloat(document.getElementById('compose-bpm').value) || 120,
        key: document.getElementById('compose-key').value || null,
        mode: document.getElementById('compose-mode').value || null,
        seed: composeSeed,
        write_midi: true,
      };
      const r = await fetch('/api/compose', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (data.error) {
        resultEl.innerHTML = `<div class="status error">${data.error}</div>`;
        resultEl.style.display = 'block';
        return;
      }
      // Render the curated session as cards
      function partCard(label, p, color) {
        if (!p) return `<div style="padding: 8px; background: var(--bg-3); border-left: 2px solid var(--fg-faint); opacity: 0.5;">
          <div style="font-size: 10px; color: var(--fg-faint); letter-spacing: 0.1em; text-transform: uppercase;">${label}</div>
          <div style="font-size: 12px; color: var(--fg-faint);">(no match in library)</div>
        </div>`;
        const tags = (p.tags || []).slice(0, 4).map(t => `<span style="background: var(--bg-2); padding: 1px 6px; border-radius: 2px; font-size: 10px;">${t}</span>`).join(' ');
        return `<div style="padding: 10px; background: var(--bg-3); border-left: 2px solid ${color};">
          <div style="font-size: 10px; color: var(--fg-faint); letter-spacing: 0.1em; text-transform: uppercase;">${label}</div>
          <div style="font-size: 13px; color: var(--fg); font-weight: 600; margin-top: 2px;">${p.name}</div>
          <div style="font-size: 10px; color: var(--fg-dim); margin: 4px 0;">${p.bpm || '—'} BPM · ${p.key || ''} ${p.key_mode || ''}</div>
          <div style="font-size: 11px; color: var(--fg-dim); margin: 4px 0;">${p.vibe || ''}</div>
          <div style="margin-top: 4px;">${tags}</div>
        </div>`;
      }
      const patchHtml = data.patch ? `<div style="padding: 10px; background: var(--bg-3); border-left: 2px solid var(--amber);">
        <div style="font-size: 10px; color: var(--fg-faint); letter-spacing: 0.1em; text-transform: uppercase;">🎹 Patch suggestion (from your bench)</div>
        <div style="font-size: 13px; color: var(--fg); font-weight: 600; margin-top: 2px;">${data.patch.instrument}/${data.patch.patch}</div>
        <div style="font-size: 10px; color: var(--fg-dim); margin: 4px 0;">match score: ${data.patch.match_score}</div>
        <div style="font-size: 11px; color: var(--fg-dim);">${data.patch.notes || ''}</div>
      </div>` : '';
      // Stash the compose data for the audition button to read.
      // Tracks: chord progression → ch1, bass → ch2, drums → ch10 (GM drum channel).
      // Each track gets its own midi_port (defaults to the current instrument's port,
      // but users can route any track to any plugged-in synth — v1 multi-synth).
      const defaultPort = getAudioSettings().midi_port || '';
      lastComposeResult = {
        bpm: parseFloat(document.getElementById('compose-bpm').value) || 120,
        tracks: [
          data.progression?.midi_path ? { midi_path: data.progression.midi_path, channel: 1,  label: 'chords', emoji: '🎹', midi_port: defaultPort } : null,
          data.bass?.midi_path        ? { midi_path: data.bass.midi_path,        channel: 2,  label: 'bass',   emoji: '🎸', midi_port: defaultPort } : null,
          data.drums?.midi_path       ? { midi_path: data.drums.midi_path,       channel: 10, label: 'drums',  emoji: '🥁', midi_port: defaultPort } : null,
        ].filter(Boolean),
      };
      const auditionable = lastComposeResult.tracks.length > 0;
      // Build per-track port dropdown options from currently-available MIDI ports
      const portOptions = (availableDevices.midi_ports || []).map(p =>
        `<option value="${p}"${p === defaultPort ? ' selected' : ''}>${p}</option>`
      ).join('');
      const trackRows = lastComposeResult.tracks.map((t, i) => `
        <div style="display: flex; gap: 8px; align-items: center; padding: 4px 0; font-size: 11px;">
          <span style="width: 16px; text-align: center;">${t.emoji}</span>
          <span style="width: 60px; color: var(--fg); font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; font-size: 10px;">${t.label}</span>
          <span style="width: 36px; color: var(--fg-faint); font-size: 10px;">ch${t.channel}</span>
          <span style="color: var(--fg-faint); font-size: 10px;">→</span>
          <select class="compose-track-port" data-track-idx="${i}" style="flex: 1; max-width: 200px; padding: 2px 4px; background: var(--bg-3); color: var(--fg); border: 1px solid var(--border); font-size: 11px;">
            ${portOptions || `<option value="${defaultPort}">${defaultPort || '(no MIDI port)'}</option>`}
          </select>
        </div>`).join('');
      const midiHtml = (data.midi_url || auditionable) ? `<div style="margin-top: 10px; padding: 10px; background: var(--bg-2); border: 1px solid var(--border);">
        ${trackRows}
        <div style="display: flex; gap: 8px; align-items: center; margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border);">
          <span style="flex: 1; font-size: 10px; color: var(--fg-faint);">tip: route each track to a different synth →</span>
          ${auditionable ? `<button class="primary" id="compose-audition-btn" style="padding: 6px 12px; font-size: 11px; margin: 0;">🎧 Audition</button>` : ''}
          ${auditionable ? `<button class="red" id="compose-audition-stop-btn" style="padding: 6px 12px; font-size: 11px; margin: 0; display: none;">■ Stop</button>` : ''}
          ${data.midi_url ? `<a href="${data.midi_url}" download style="padding: 6px 12px; background: var(--amber); color: #000; text-decoration: none; font-size: 11px; font-weight: 700; border-radius: 2px;">⬇ Download .mid</a>` : ''}
        </div>
      </div>` : '';
      resultEl.innerHTML = `
        <div style="font-size: 10px; color: var(--fg-faint); margin-bottom: 8px;">
          Genre: <b>${data.genre}</b> · BPM range: ${data.bpm_range[0]}–${data.bpm_range[1]} · Key: ${data.key || 'any'} ${data.mode || ''}
        </div>
        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px;">
          ${partCard('CHORD PROGRESSION', data.progression, 'var(--green, #5fb866)')}
          ${partCard('BASS LINE', data.bass, '#5b9fd9')}
          ${partCard('DRUM PATTERN', data.drums, '#d96b5b')}
        </div>
        <div style="margin-top: 8px;">${patchHtml}</div>
        ${midiHtml}
      `;
      resultEl.style.display = 'block';
      // Wire the freshly-rendered audition buttons (they only exist after compose returns)
      wireComposeAuditionButtons();
    } catch (e) {
      resultEl.innerHTML = `<div class="status error">Compose failed: ${e.message}</div>`;
      resultEl.style.display = 'block';
    } finally {
      btn.disabled = false;
      btn.textContent = '🧬 Compose';
    }
  }

  // Wire the audition + stop buttons on the compose result card. These elements
  // are rendered fresh every time compose returns, so we re-attach on each render.
  function wireComposeAuditionButtons() {
    const auditionBtn = document.getElementById('compose-audition-btn');
    const stopBtn = document.getElementById('compose-audition-stop-btn');
    if (!auditionBtn || !stopBtn) return;

    // v1 multi-synth: each track's port dropdown writes back to lastComposeResult.
    // So when the user changes "drums" from mio → TR-8S, drums route there on next audition.
    document.querySelectorAll('.compose-track-port').forEach(sel => {
      sel.addEventListener('change', (e) => {
        const idx = parseInt(e.target.dataset.trackIdx, 10);
        if (lastComposeResult && lastComposeResult.tracks[idx]) {
          lastComposeResult.tracks[idx].midi_port = e.target.value;
        }
      });
    });

    auditionBtn.addEventListener('click', async () => {
      if (!lastComposeResult || !lastComposeResult.tracks.length) {
        setStatus('No compose result to audition', 'error');
        return;
      }
      // Backend wants {midi_path, channel, midi_port} per track. Top-level midi_port
      // is the fallback for any track that didn't explicitly choose one.
      const defaultPort = getAudioSettings().midi_port || '';
      const trackPorts = lastComposeResult.tracks.map(t => t.midi_port || defaultPort);
      const anyMissing = trackPorts.some(p => !p);
      if (anyMissing) {
        setStatus('Each track needs a MIDI port — pick an instrument or set ports per track', 'error');
        return;
      }
      auditionBtn.disabled = true;
      auditionBtn.textContent = '⏳ Starting…';
      try {
        const r = await fetch('/api/audition-compose', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            tracks: lastComposeResult.tracks.map((t, i) => ({
              midi_path: t.midi_path,
              channel: t.channel,
              midi_port: trackPorts[i],
            })),
            bpm: lastComposeResult.bpm,
            midi_port: defaultPort,
          }),
        });
        const data = await r.json();
        if (data.ok) {
          auditionBtn.style.display = 'none';
          stopBtn.style.display = 'inline-block';
          startAuditionKeepalive();
          // Show which synth each track went to — supports multi-synth case
          const portsList = (data.ports_used || []).join(', ');
          const trackSummary = data.tracks.map(t => `${t.path.split('/').pop().replace('.mid','')}→${t.port_resolved || t.port_requested}`).join(' · ');
          setStatus(`🎧 Auditioning: ${trackSummary} · loop ${data.loop_sec}s · ports: ${portsList}`, 'success');
        } else {
          setStatus(`Audition failed: ${data.error || 'unknown'}`, 'error');
          auditionBtn.disabled = false;
          auditionBtn.textContent = '🎧 Audition';
        }
      } catch (e) {
        setStatus(`Audition error: ${e.message}`, 'error');
        auditionBtn.disabled = false;
        auditionBtn.textContent = '🎧 Audition';
      }
    });
    stopBtn.addEventListener('click', async () => {
      stopBtn.textContent = '⏳';
      try {
        await fetch('/api/audition-stop', { method: 'POST' });
      } catch (e) { /* noop */ }
      stopAuditionKeepalive();
      stopBtn.style.display = 'none';
      auditionBtn.style.display = 'inline-block';
      auditionBtn.disabled = false;
      auditionBtn.textContent = '🎧 Audition';
      stopBtn.textContent = '■ Stop';
      setStatus('Audition stopped.', '');
    });
  }

  document.getElementById('compose-btn')?.addEventListener('click', () => {
    composeSeed = null;  // fresh seed each time
    runCompose();
  });
  document.getElementById('compose-shuffle-btn')?.addEventListener('click', () => {
    composeSeed = Math.floor(Math.random() * 100000);
    runCompose();
  });
  loadComposerGenres();

  // ---------- init ----------
  refreshDevices().then(() => {
    loadInstruments();
  });
  loadSavedProgressions();
  loadDrumPatterns();
  loadCapturesLog();
  setInterval(loadCapturesLog, 10000);  // refresh every 10 sec

  // ---------- computer-keyboard player — audition a patch without walking to the synth ----------
  (function keyboardPlayer() {
    const BASE = 48;                                   // 'A' = C3 (MIDI 48); octave buttons shift by 12
    const MAP = { a:0, w:1, s:2, e:3, d:4, f:5, t:6, g:7, y:8, h:9, u:10, j:11, k:12, o:13, l:14, p:15 };
    const KB = { on:false, octave:0, chord:false, held:{} };
    const noteName = (n) => ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'][((n%12)+12)%12] + (Math.floor(n/12)-1);
    const bar = document.createElement('div');
    bar.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:9999;display:flex;align-items:center;gap:14px;padding:6px 12px;background:var(--bg-3,#15171c);border-top:1px solid var(--border,#2a2e37);font-family:var(--mono,monospace);font-size:11px;color:var(--fg-faint,#8a8fa3);';
    bar.innerHTML =
      '<button id="kb-toggle" style="padding:4px 12px;font-weight:700;border-radius:3px;cursor:pointer;border:1px solid var(--border,#2a2e37);">⌨ keys: OFF</button>'
      + '<span>oct <button id="kb-dn" style="padding:1px 8px;cursor:pointer;">−</button> <b id="kb-oct" style="color:var(--fg,#e2e8f2);">0</b> <button id="kb-up" style="padding:1px 8px;cursor:pointer;">+</button></span>'
      + '<button id="kb-chord" style="padding:3px 10px;cursor:pointer;border:1px solid var(--border,#2a2e37);border-radius:3px;">chord: off</button>'
      + '<span style="opacity:0.7;">A W S E D F T G Y H U J K · Z/X octave</span>'
      + '<span id="kb-note" style="margin-left:auto;color:var(--amber,#F2B705);font-weight:700;"></span>';
    document.body.appendChild(bar);
    const $ = (id) => document.getElementById(id);
    const upd = () => {
      $('kb-oct').textContent = (KB.octave>0?'+':'') + KB.octave;
      $('kb-toggle').textContent = '⌨ keys: ' + (KB.on?'ON':'OFF');
      $('kb-toggle').style.background = KB.on ? 'var(--amber,#F2B705)' : 'var(--bg-2,#1c1f26)';
      $('kb-toggle').style.color = KB.on ? '#1a0e00' : 'var(--fg-faint,#8a8fa3)';
      $('kb-chord').textContent = 'chord: ' + (KB.chord?'maj':'off');
    };
    async function send(notes, on) {
      const port = (typeof getAudioSettings === 'function' ? (getAudioSettings().midi_port) : null);
      if (!port) { $('kb-note').textContent = 'pick an instrument first'; return; }
      if (on) $('kb-note').textContent = notes.map(noteName).join('  ');
      try { await fetch('/api/note', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ midi_port: port, channel: 1, notes, on, velocity: 100 }) }); } catch (e) {}
    }
    $('kb-toggle').onclick = () => { KB.on = !KB.on; upd(); };
    $('kb-dn').onclick = () => { KB.octave = Math.max(-3, KB.octave-1); upd(); };
    $('kb-up').onclick = () => { KB.octave = Math.min(3, KB.octave+1); upd(); };
    $('kb-chord').onclick = () => { KB.chord = !KB.chord; upd(); };
    upd();
    document.addEventListener('keydown', (e) => {
      if (!KB.on || e.metaKey || e.ctrlKey || e.altKey) return;
      const tag = ((document.activeElement && document.activeElement.tagName) || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;   // don't hijack typing
      const k = e.key.toLowerCase();
      if (k === 'z') { KB.octave = Math.max(-3, KB.octave-1); upd(); e.preventDefault(); return; }
      if (k === 'x') { KB.octave = Math.min(3, KB.octave+1); upd(); e.preventDefault(); return; }
      if (!(k in MAP) || KB.held[k]) return;            // unknown key or key-repeat
      e.preventDefault();
      const root = BASE + MAP[k] + KB.octave * 12;
      const notes = KB.chord ? [root, root+4, root+7] : [root];   // maj triad in chord mode
      KB.held[k] = notes; send(notes, true);
    });
    document.addEventListener('keyup', (e) => {
      const k = e.key.toLowerCase();
      if (KB.held[k]) { send(KB.held[k], false); delete KB.held[k]; }
    });
    window.addEventListener('blur', () => { Object.values(KB.held).forEach(n => send(n, false)); KB.held = {}; });
  })();
})();

// ---------- Play Along: session key → in-key chord progressions ----------
// Set a root + scale; the 🎲 button writes a diatonic progression into the chord box. Pair it with
// the clock (synced to your DAW) and the bench plays in TIME and in KEY — the sister-software move.
// Self-contained: only touches its own panel + the prog-chords box.
(function () {
  const NOTES = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'Gb', 'G', 'Ab', 'A', 'Bb', 'B'];
  const SCALES = {
    major:            { steps: [0, 2, 4, 5, 7, 9, 11], quals: ['', 'm', 'm', '', '', 'm', 'dim'] },
    minor:            { steps: [0, 2, 3, 5, 7, 8, 10], quals: ['m', 'dim', '', 'm', 'm', '', ''] },
    dorian:           { steps: [0, 2, 3, 5, 7, 9, 10], quals: ['m', 'm', '', '', 'm', 'dim', ''] },
    phrygian:         { steps: [0, 1, 3, 5, 7, 8, 10], quals: ['m', '', '', 'm', 'dim', '', 'm'] },
    mixolydian:       { steps: [0, 2, 4, 5, 7, 9, 10], quals: ['', 'm', 'dim', '', 'm', 'm', ''] },
    'harmonic-minor': { steps: [0, 2, 3, 5, 7, 8, 11], quals: ['m', 'dim', '', 'm', '', '', 'dim'] },
  };
  // Progression templates as scale-degree indices. For major/minor they dodge the diminished degree;
  // modes (dorian etc.) may surface a diatonic dim chord, which is in-key and the fire engine parses fine.
  const TEMPLATES = {
    minorish: [[0, 5, 2, 6], [0, 3, 4, 0], [0, 6, 5, 6], [0, 3, 5, 4]],  // i-VI-III-VII, i-iv-v-i, i-VII-VI-VII, i-iv-VI-v
    majorish: [[0, 4, 5, 3], [0, 5, 3, 4], [0, 3, 4, 0], [1, 4, 0, 0]],  // I-V-vi-IV, I-vi-IV-V, I-IV-V-I, ii-V-I
  };
  const MAJORISH = ['major', 'mixolydian', 'lydian'];
  let tmplIdx = 0;

  function setup() {
    const rootEl = document.getElementById('session-root');
    const scaleEl = document.getElementById('session-scale');
    const genEl = document.getElementById('session-gen');
    const readEl = document.getElementById('session-key-readout');
    if (!rootEl || !scaleEl) return;   // panel not on this build

    const diatonic = (rootPc, scale) => {
      const sc = SCALES[scale] || SCALES.minor;
      return sc.steps.map((st, i) => NOTES[(rootPc + st) % 12] + sc.quals[i]);
    };
    const sync = () => {
      const root = rootEl.value, scale = scaleEl.value;
      localStorage.setItem('benchSessionRoot', root);
      localStorage.setItem('benchSessionScale', scale);
      const rootPc = NOTES.indexOf(root);
      if (rootPc >= 0 && readEl) readEl.textContent = `in ${root} ${scale.replace('-', ' ')}:  ` + diatonic(rootPc, scale).join('  ');
    };
    const generate = () => {
      const rootPc = NOTES.indexOf(rootEl.value);
      if (rootPc < 0) return;
      const dia = diatonic(rootPc, scaleEl.value);
      const fam = MAJORISH.includes(scaleEl.value) ? TEMPLATES.majorish : TEMPLATES.minorish;
      const tmpl = fam[tmplIdx % fam.length]; tmplIdx++;
      const box = document.getElementById('prog-chords');
      if (box) { box.value = tmpl.map(d => dia[d]).join(' '); box.dispatchEvent(new Event('input', { bubbles: true })); }
    };

    const sr = localStorage.getItem('benchSessionRoot');
    if (sr && [...rootEl.options].some(o => o.value === sr || o.text === sr)) rootEl.value = sr;
    const ss = localStorage.getItem('benchSessionScale');
    if (ss && [...scaleEl.options].some(o => o.value === ss)) scaleEl.value = ss;
    rootEl.addEventListener('change', sync);
    scaleEl.addEventListener('change', sync);
    if (genEl) genEl.addEventListener('click', generate);
    sync();
  }
  if (document.readyState !== 'loading') setup();
  else document.addEventListener('DOMContentLoaded', setup);
})();
