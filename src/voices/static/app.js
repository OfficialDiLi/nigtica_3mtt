document.addEventListener('DOMContentLoaded', () => {
  // ===== Tab Switching =====
  const tabs = document.querySelectorAll('.tab');
  const sections = document.querySelectorAll('.section');
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      tabs.forEach(t => t.classList.remove('active'));
      sections.forEach(s => s.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById(tab.dataset.tab + '-section').classList.add('active');
    });
  });

  // ===== Toast =====
  function showToast(msg) {
    const toast = document.getElementById('toast');
    toast.textContent = msg;
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 2500);
  }

  function setStatus(text) {
    document.getElementById('statusText').textContent = text;
  }

  // ===== History =====
  const history = [];
  function addToHistory(type, text, lang) {
    history.unshift({ type, text, lang, time: new Date() });
    if (history.length > 20) history.pop();
    renderHistory();
  }

  function renderHistory() {
    const list = document.getElementById('history-list');
    if (history.length === 0) {
      list.innerHTML = '<div class="empty-history"><div>No history yet.<br>Start speaking or typing!</div></div>';
      return;
    }
    list.innerHTML = history.map(h => `
      <div class="history-item">
        <div class="history-meta">
          <span class="history-type">${h.type}</span>
          <span class="history-time">${h.time.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}</span>
        </div>
        <div class="history-text">${escapeHtml(h.text.substring(0, 150))}${h.text.length > 150 ? '...' : ''}</div>
        <div class="history-lang">${escapeHtml(h.lang)}</div>
      </div>
    `).join('');
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function formatErr(o, fallback) {
    let msg = (o && (o.user_message || o.error)) || fallback || 'Request failed';
    const det = o && o.details;
    if (det) {
      if (Array.isArray(det.errors) && det.errors.length) {
        msg += ' — ' + det.errors.map(e => (typeof e === 'string') ? e : JSON.stringify(e)).join('; ');
      } else if (typeof det === 'object' && Object.keys(det).length) {
        msg += ' — ' + JSON.stringify(det).slice(0, 300);
      }
    }
    if (o && o.error) msg += ' [' + o.error + ']';
    return msg;
  }

  // ===== API key state + voices =====
  let apiReady = true;
  const csrfToken = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

  function requireAuth(job) {
    if (job && job.error === 'AUTH_REQUIRED') {
      window.location = '/login?next=' + encodeURIComponent('/app');
      return true;
    }
    return false;
  }

  function feedbackButtons(container, payload) {
    var wrap = document.createElement('div');
    wrap.className = 'feedback-row';
    wrap.textContent = 'Was this good? ';
    ['up', 'down'].forEach(function (v) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'chip';
      b.textContent = v === 'up' ? '👍' : '👎';
      b.setAttribute('aria-label', v === 'up' ? 'Good result' : 'Bad result');
      b.addEventListener('click', function () {
        fetch('/api/feedback', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
          body: JSON.stringify({ kind: payload.kind, engine: payload.engine, language: payload.language, job_id: payload.job_id || '', vote: v })
        }).catch(function () {});
        wrap.textContent = 'Thanks for the feedback!';
      });
      wrap.appendChild(b);
    });
    container.appendChild(wrap);
  }

  var deleteLink = document.getElementById('delete-account');
  if (deleteLink) {
    deleteLink.addEventListener('click', function (e) {
      e.preventDefault();
      if (!confirm('Delete your account and sign out? This cannot be undone.')) return;
      fetch('/api/account/delete', { method: 'POST', headers: { 'X-CSRF-Token': csrfToken } })
        .then(function () { window.location = '/login'; })
        .catch(function () { showToast('Delete failed — try again'); });
    });
  }
  fetch('/health').then(r => r.json()).then(h => {
    apiReady = !!h.api_key_configured;
    if (!apiReady) {
      document.getElementById('key-warning').style.display = 'block';
      setStatus('No API key');
    }
  }).catch(() => {});

  const voiceSel = document.getElementById('tts-voice');
  const voiceNote = document.getElementById('voice-note');
  fetch('/api/voices').then(r => r.json()).then(d => {
    if (d && d.error) { voiceNote.textContent = 'Voices: ' + (d.user_message || d.error); return; }
    const items = Array.isArray(d) ? d : (d.voices || []);
    if (!Array.isArray(items) || items.length === 0) {
      voiceNote.textContent = (d && d.hint) || 'No voices listed — using provider default.';
      return;
    }
    items.forEach(v => {
      const name = (typeof v === 'string') ? v : v.name;
      if (!name) return;
      const label = ((typeof v === 'object') && v.default) ? name + ' (default)' : name;
      const o = document.createElement('option');
      o.value = name; o.textContent = label;
      voiceSel.appendChild(o);
    });
  }).catch(() => { voiceNote.textContent = 'Could not load voices — using provider default.'; });

  // ===== TTS (YarnGPT job + poll) =====
  const ttsText = document.getElementById('tts-text');
  const charCount = document.getElementById('charCount');
  const ttsRate = document.getElementById('tts-rate');
  const rateVal = document.getElementById('rateVal');
  const speakBtn = document.getElementById('tts-speak');
  const pauseBtn = document.getElementById('tts-pause');
  const stopBtn = document.getElementById('tts-stop');
  const playerBox = document.getElementById('tts-player');
  let currentAudio = null;

  ttsText.addEventListener('input', () => { charCount.textContent = ttsText.value.length; });
  charCount.textContent = ttsText.value.length;
  document.querySelectorAll('[data-sample]').forEach(btn => {
    btn.addEventListener('click', () => {
      ttsText.value = btn.dataset.sample;
      charCount.textContent = ttsText.value.length;
      ttsText.focus();
    });
  });
  ttsRate.addEventListener('input', () => {
    rateVal.textContent = parseFloat(ttsRate.value).toFixed(1) + 'x';
    if (currentAudio) currentAudio.playbackRate = parseFloat(ttsRate.value);
  });

  function showTtsError(msg) {
    playerBox.innerHTML = '';
    const div = document.createElement('div');
    div.className = 'error-box';
    div.textContent = msg;
    playerBox.appendChild(div);
  }

  speakBtn.addEventListener('click', async () => {
    const text = ttsText.value.trim();
    if (!text) { showToast('Please enter some text'); return; }
    if (text.length > 10000) { showToast('Text exceeds 10,000 characters'); return; }
    if (!apiReady) { showToast('Set YARNGPT_API_KEY first'); return; }
    speakBtn.disabled = true;
    setStatus('Queueing...');
    const fd = new FormData();
    fd.append('text', text);
    fd.append('voice', document.getElementById('tts-voice').value);
    fd.append('output_format', document.getElementById('tts-format').value);
    fd.append('target_language', document.getElementById('tts-target').value);
    let job;
    try {
      job = await (await fetch('/api/tts', { method: 'POST', headers: { 'X-CSRF-Token': csrfToken }, body: fd })).json();
    } catch (e) {
      showTtsError('Network error queueing synthesis.');
      speakBtn.disabled = false; setStatus('Ready'); return;
    }
    if (requireAuth(job)) return;
    if (job.error) {
      showTtsError(formatErr(job));
      speakBtn.disabled = false; setStatus('Ready'); return;
    }
    setStatus('Synthesizing...');
    const label = voiceSel.options[voiceSel.selectedIndex].text;
    await pollTts(job.job_id, { text, label });
    speakBtn.disabled = false;
  });

  async function pollTts(jobId, meta) {
    const t0 = Date.now();
    while (Date.now() - t0 < 120000) {
      await new Promise(r => setTimeout(r, 2000));
      let s;
      try {
        s = await (await fetch('/api/tts-job/' + encodeURIComponent(jobId))).json();
      } catch (e) { continue; }
      if (s.error) { showTtsError(formatErr(s)); setStatus('Ready'); return; }
      if (s.status === 'completed' && s.audio_url) {
        playerBox.innerHTML = '';
        const audio = document.createElement('audio');
        audio.controls = true;
        audio.src = s.audio_url;
        audio.playbackRate = parseFloat(ttsRate.value);
        const link = document.createElement('a');
        link.href = s.audio_url;
        link.textContent = 'Download audio';
        link.setAttribute('download', '');
        playerBox.appendChild(audio);
        playerBox.appendChild(link);
        currentAudio = audio;
        audio.play().catch(() => {});
        addToHistory('TTS', meta.text, meta.label);
        feedbackButtons(playerBox, { kind: 'tts', engine: 'yarngpt', language: meta.label, job_id: jobId });
        setStatus('Ready');
        showToast('Audio ready');
        return;
      }
      if (s.status === 'failed') {
        showTtsError(formatErr(s, 'Synthesis failed.'));
        setStatus('Ready');
        return;
      }
      setStatus('Synthesizing... ' + (s.percentage || 0) + '%');
    }
    showTtsError('Synthesis is taking longer than expected — reload and try again.');
    setStatus('Ready');
  }

  pauseBtn.addEventListener('click', () => {
    if (!currentAudio) { showToast('Nothing to pause yet'); return; }
    if (currentAudio.paused) { currentAudio.play().catch(() => {}); pauseBtn.lastChild.textContent = ' Pause'; setStatus('Playing...'); }
    else { currentAudio.pause(); pauseBtn.lastChild.textContent = ' Resume'; setStatus('Paused'); }
  });

  stopBtn.addEventListener('click', () => {
    if (currentAudio) { currentAudio.pause(); currentAudio.currentTime = 0; }
    pauseBtn.lastChild.textContent = ' Pause';
    setStatus('Ready');
  });

  // ===== STT (mic recording via MediaRecorder + YarnGPT) =====
  const sttDisplay = document.getElementById('stt-display');
  const sttPlaceholder = document.getElementById('stt-placeholder');
  const sttResult = document.getElementById('stt-result');
  const sttLang = document.getElementById('stt-target');
  const waveBars = document.querySelectorAll('.wave-bar');
  const startBtn = document.getElementById('stt-start');
  const stopRecBtn = document.getElementById('stt-stop');
  let mediaRecorder = null;
  let audioChunks = [];
  let micStream = null;

  function setRecordingUI(on) {
    startBtn.style.display = on ? 'none' : 'inline-flex';
    stopRecBtn.style.display = on ? 'inline-flex' : 'none';
    sttDisplay.classList.toggle('recording', on);
    waveBars.forEach(b => b.classList.toggle('active', on));
  }

  startBtn.addEventListener('click', async () => {
    if (!apiReady) { showToast('Set YARNGPT_API_KEY first'); return; }
    if (!window.MediaRecorder) { showToast('Recording not supported in this browser'); return; }
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      showToast('Microphone access denied');
      return;
    }
    audioChunks = [];
    const mime = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : '';
    mediaRecorder = mime ? new MediaRecorder(micStream, { mimeType: mime }) : new MediaRecorder(micStream);
    mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) audioChunks.push(e.data); };
    mediaRecorder.onstop = uploadRecording;
    mediaRecorder.start();
    sttPlaceholder.style.display = 'none';
    sttResult.textContent = 'Listening...';
    sttDisplay.classList.remove('empty');
    setRecordingUI(true);
    setStatus('Listening...');
  });

  stopRecBtn.addEventListener('click', () => {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
    if (micStream) micStream.getTracks().forEach(t => t.stop());
    setRecordingUI(false);
    setStatus('Uploading...');
  });

  async function uploadRecording() {
    const blob = new Blob(audioChunks, { type: mediaRecorder.mimeType || 'audio/webm' });
    if (!blob.size) { sttResult.textContent = 'No audio captured.'; setStatus('Ready'); return; }
    sttResult.textContent = 'Transcribing...';
    const fd = new FormData();
    fd.append('file', blob, 'recording.webm');
    fd.append('engine', document.getElementById('stt-engine').value);
    fd.append('language', document.getElementById('stt-language').value);
    fd.append('target_language', sttLang.value);
    let job;
    try {
      job = await (await fetch('/api/stt', { method: 'POST', headers: { 'X-CSRF-Token': csrfToken }, body: fd })).json();
    } catch (e) {
      sttResult.textContent = 'Network error uploading audio.';
      setStatus('Ready'); return;
    }
    if (requireAuth(job)) return;
    if (job.error) {
      sttResult.textContent = formatErr(job);
      setStatus('Ready'); return;
    }
    if (job.status === 'completed' && job.transcript !== undefined) {
      renderSttDone(job, job.job_id || null);
      return;
    }
    await pollStt(job.job_id);
  }

  function renderSttDone(s, jobId) {
    let text = s.transcript || '(silence)';
    if (s.target_language && s.translation_status === 'completed') {
      text += '\n\n[' + s.target_language + '] ' + (s.translated_transcript || '(empty)');
    }
    sttResult.textContent = text;
    const langSel = document.getElementById('stt-language');
    const label = s.engine === 'groq'
      ? ('groq whisper (' + (s.language || langSel.value) + ')')
      : (sttLang.value ? ('translated to ' + sttLang.value) : 'transcription');
    addToHistory('STT', s.transcript || '(silence)', label);
    feedbackButtons(sttDisplay, {
      kind: 'stt',
      engine: s.engine || document.getElementById('stt-engine').value,
      language: s.language || langSel.value,
      job_id: jobId || ''
    });
    setStatus('Ready');
    showToast('Transcription ready');
  }

  async function pollStt(jobId) {
    const t0 = Date.now();
    while (Date.now() - t0 < 300000) {
      await new Promise(r => setTimeout(r, 3000));
      let s;
      try {
        s = await (await fetch('/api/stt-job/' + encodeURIComponent(jobId))).json();
      } catch (e) { continue; }
      if (s.error) { sttResult.textContent = formatErr(s); setStatus('Ready'); return; }
      if (s.status === 'completed') {
        renderSttDone(s, jobId);
        return;
      }
      if (s.status === 'failed') {
        let msg = s.error_message || 'Transcription failed.';
        msg += s.audio_available ? ' You can retry this upload.' : ' The audio was cleaned up — record again.';
        sttResult.textContent = msg;
        setStatus('Ready');
        return;
      }
      sttResult.textContent = 'Transcribing... (' + s.status + ')';
    }
    sttResult.textContent = 'Transcription is taking too long — try again.';
    setStatus('Ready');
  }

  document.getElementById('stt-clear').addEventListener('click', () => {
    sttResult.textContent = '';
    sttPlaceholder.style.display = 'inline';
    sttDisplay.classList.add('empty');
    showToast('Transcription cleared');
  });

  document.getElementById('stt-copy').addEventListener('click', () => {
    const text = sttResult.textContent.trim();
    if (!text) { showToast('Nothing to copy'); return; }
    navigator.clipboard.writeText(text).then(() => {
      showToast('Copied to clipboard!');
    }).catch(() => { showToast('Copy failed'); });
  });
});
