import 'ui-flip-glass/auto';
import { enhanceGlass, showToast } from 'ui-flip-glass';
import './style.css';

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const phaseLabels = {
  initializing: '初始化',
  ready: '就绪',
  preparing: '准备中',
  recording: '录制中',
  paused: '已暂停',
  finalizing: '整理中',
  importing: '导入中',
  injecting: '写入中',
  cancelling: '取消中',
  transcribing: '识别中',
  error: '异常',
};

const defaultState = {
  connected: false,
  status: {
    phase: 'ready',
    message: '准备就绪',
    detail: '所有语音处理均在本机完成',
    running: false,
    recording: false,
    paused: false,
    elapsed_seconds: 0,
    partial_segments: 0,
    allowed_actions: {},
  },
  transcript: { text: '', editable: false, streaming: false, session_id: '', segments: [], revision: 0, dirty: false, save_state: 'saved' },
  settings: {
    hotkey: 'Ctrl+Shift+F8',
    start_minimized: false,
    floating_window: true,
    inject_text: false,
    enable_hotkey: true,
    minimize_to_tray: true,
    launch_on_startup: false,
    recognition_language: 'zh-Hans',
    microphone_device: '',
    transcript_editable: false,
    streaming_transcription_enabled: true,
    lexicon_enabled: true,
    lexicon_path: '',
    enabled_builtin_lexicons: [],
  },
  options: {
    languages: [
      { value: 'zh-Hans', label: '中文（简体）' },
      { value: 'zh-Hant', label: '中文（繁体）' },
      { value: 'en', label: '英语' },
    ],
    lexicons: [],
    microphones: [],
  },
  audio: {
    level: 0,
    microphone: { status: 'idle', device: null, name: '系统默认麦克风', message: '' },
  },
  history: [],
  capabilities: {
    max_recording_seconds: 7200,
    runtime: '本地 FunASR',
    model_profile: 'full',
    audio_import: true,
    streaming: true,
    structured_history: true,
  },
};

let state = structuredClone(defaultState);
let bridge = null;
let transcriptTimer = 0;
let localTranscriptText = null;
let mockTimer = 0;
let lastRenderedText = '';
let selectedHistoryId = '';
let requestedHistoryId = '';
let streamingBaseText = '';
let streamingPreviewText = '';
let activeDialogRoot = null;
let dialogReturnFocus = null;
let settingsDraft = null;
let settingsDirty = false;
let confirmationResolve = null;
const pendingCommands = new Map();
const historyDetails = new Map();

function readSidebarPreference() {
  try {
    return window.localStorage.getItem('voice-sidebar-collapsed') === 'true';
  } catch {
    return false;
  }
}

function setSidebarCollapsed(collapsed, persist = true) {
  document.body.classList.toggle('sidebar-collapsed', collapsed);
  const button = $('#collapseSidebarButton');
  button.setAttribute('aria-expanded', String(!collapsed));
  button.setAttribute('aria-label', collapsed ? '展开会话侧栏' : '收起会话侧栏');
  button.title = collapsed ? '展开侧栏' : '收起侧栏';
  if (!persist) return;
  try {
    window.localStorage.setItem('voice-sidebar-collapsed', String(collapsed));
  } catch {
    // The UI remains usable when persistent storage is unavailable.
  }
}

function setCompactPanel(open) {
  document.body.classList.toggle('compact-panel-open', open);
  $('#compactPanelButton').setAttribute('aria-expanded', String(open));
  $('#compactPanelButton').setAttribute('aria-label', open ? '关闭会话面板' : '打开会话面板');
}

function callBridge(method, ...args) {
  if (bridge && typeof bridge[method] === 'function') {
    bridge[method](...args);
  }
}

function actionAllowed(name, fallback) {
  const actions = state.status.allowed_actions;
  if (actions && Object.hasOwn(actions, name)) return Boolean(actions[name]);
  return Boolean(fallback);
}

function commandPending(name) {
  return pendingCommands.has(name);
}

function runCommand(name, method, ...args) {
  if (commandPending(name)) return;
  const timeout = window.setTimeout(() => {
    pendingCommands.delete(name);
    render();
  }, 2500);
  pendingCommands.set(name, timeout);
  render();
  callBridge(method, ...args);
}

function settlePendingCommands() {
  if (!pendingCommands.size) return;
  pendingCommands.forEach((timeout) => window.clearTimeout(timeout));
  pendingCommands.clear();
}

function parsePayload(payload) {
  try {
    return typeof payload === 'string' ? JSON.parse(payload) : payload;
  } catch (error) {
    console.error('invalid runtime payload', error);
    return null;
  }
}

function formatClock(totalSeconds) {
  const total = Math.max(0, Math.floor(Number(totalSeconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return [hours, minutes, seconds].map((value) => String(value).padStart(2, '0')).join(':');
  return [minutes, seconds].map((value) => String(value).padStart(2, '0')).join(':');
}

function languageLabel(value) {
  return state.options.languages.find((item) => item.value === value)?.label || value || '中文（简体）';
}

function modeLabel() {
  if (state.settings.streaming_transcription_enabled) return '实时流式';
  return state.status.partial_segments ? `分段转写 · ${state.status.partial_segments} 段` : '结束后转写';
}

function setButtonEnabled(selector, enabled) {
  const button = $(selector);
  if (button) button.disabled = !enabled;
}

function render() {
  const { status, transcript, settings } = state;
  const text = localTranscriptText ?? transcript.text ?? '';
  const hasText = Boolean(text.trim());
  const phase = status.phase || 'ready';
  const phaseLabel = phaseLabels[phase] || phase;

  document.body.dataset.phase = phase;
  document.body.classList.toggle('is-running', status.running);
  document.body.classList.toggle('is-recording', status.recording && !status.paused);
  document.body.classList.toggle('is-paused', status.paused);

  $('#connectionDot').classList.toggle('is-connected', state.connected);
  $('#connectionText').textContent = state.connected ? '本地运行时已连接' : '界面预览模式';
  $('#phaseBadge').textContent = phaseLabel;
  $('#phaseBadge').dataset.phase = phase;
  renderClock(status.elapsed_seconds);
  $('#languageValue').textContent = languageLabel(settings.recognition_language);
  $('#modeValue').textContent = modeLabel();
  $('#dockMode').textContent = modeLabel();
  $('#lexiconValue').textContent = settings.lexicon_enabled ? '已启用' : '未启用';
  $('#statusHeadline').textContent = status.message || '准备就绪';
  $('#statusDetail').textContent = status.detail || '所有语音处理均在本机完成';
  $('#sessionCaption').textContent = `单次录制最长 ${Math.round((state.capabilities.max_recording_seconds || 7200) / 3600)} 小时`;
  const segmentCount = Array.isArray(transcript.segments) ? transcript.segments.length : 0;
  const segmentLabel = segmentCount ? ` · ${segmentCount} 段` : '';
  $('#transcriptMeta').textContent = `${text.trim().length} 字${segmentLabel} · ${state.capabilities.runtime || '本地处理'}`;
  $('#historyCount').textContent = String(state.history.length);
  renderAudioLevel(state.audio.level);
  renderMicrophoneState();

  const editor = $('#transcriptEditor');
  if (editor.value !== text && text !== lastRenderedText) {
    const selectionStart = editor.selectionStart;
    const selectionEnd = editor.selectionEnd;
    const scrollTop = editor.scrollTop;
    const atEnd = editor.selectionStart === editor.value.length && editor.selectionEnd === editor.value.length;
    editor.value = text;
    if (atEnd) {
      editor.setSelectionRange(text.length, text.length);
    } else {
      editor.setSelectionRange(Math.min(selectionStart, text.length), Math.min(selectionEnd, text.length));
    }
    editor.scrollTop = scrollTop;
  }
  lastRenderedText = text;
  const editorEnabled = actionAllowed('edit_transcript', settings.transcript_editable && !status.running);
  editor.readOnly = !editorEnabled;
  editor.classList.toggle('is-editable', editorEnabled);
  $('#editorEmpty').hidden = hasText;
  $('#streamingIndicator').hidden = !transcript.streaming;
  renderCurrentSegments(transcript.segments);

  setButtonEnabled('#copyButton', hasText);
  setButtonEnabled('#copyDockButton', hasText);
  setButtonEnabled('#findButton', hasText);
  setButtonEnabled('#clearButton', hasText && actionAllowed('clear_transcript', !status.running) && !commandPending('clear'));
  setButtonEnabled('#exportButton', hasText);
  const canStart = actionAllowed('start_recording', !status.running);
  const canStop = actionAllowed('stop_recording', status.recording);
  const canCancel = actionAllowed('cancel_cycle', status.running && !status.recording);
  setButtonEnabled('#recordButton', (canStart || canStop || canCancel) && !commandPending('primary'));
  setButtonEnabled('#pauseButton', actionAllowed(status.paused ? 'resume_recording' : 'pause_recording', status.recording) && !commandPending('pause'));
  const canImportAudio = state.capabilities.audio_import !== false;
  ['#importButton', '#importSideButton'].forEach((selector) => {
    const button = $(selector);
    button.hidden = !canImportAudio;
    button.disabled = !actionAllowed('import_audio', !status.running) || !canImportAudio || commandPending('import');
    button.setAttribute('aria-disabled', String(button.disabled));
  });

  $('#pauseButton').classList.toggle('is-resume', status.paused);
  $('#pauseButton').setAttribute('aria-label', status.paused ? '继续录音' : '暂停录音');
  $('#pauseButton').title = status.paused ? '继续录音' : '暂停录音';
  $('#recordButton').classList.toggle('is-stop', status.recording || canCancel);
  $('#recordButton').classList.toggle('is-cancel', canCancel);
  const recordButtonLabel = status.recording ? '结束录制' : canCancel ? '取消当前任务' : status.running ? '处理中' : '开始录制';
  $('#recordButton').setAttribute('aria-label', recordButtonLabel);
  $('#recordButton').title = recordButtonLabel;

  if (status.recording) {
    $('#dockTitle').textContent = status.paused ? '录音已暂停' : '正在聆听';
    $('#dockSubtitle').textContent = status.paused ? '点击继续后恢复实时转写' : '说话内容会实时出现在转写区';
  } else if (status.running) {
    const runningCopy = {
      importing: ['正在识别音频', '本地模型正在处理导入文件'],
      injecting: ['正在写入文字', '识别结果正在发送到目标输入框'],
      cancelling: ['正在取消', '正在停止识别并清理本地资源'],
      finalizing: ['正在整理文字', '本地模型正在完成最终复核'],
    }[phase] || ['正在处理', status.detail || '本地识别模型正在运行'];
    [$('#dockTitle').textContent, $('#dockSubtitle').textContent] = runningCopy;
  } else {
    $('#dockTitle').textContent = '准备就绪';
    const hotkeyReady = settings.enable_hotkey && status.hotkey_registered !== false;
    $('#dockSubtitle').textContent = hotkeyReady ? `按 ${settings.hotkey || 'Ctrl+Shift+F8'} 也可以开始` : '点击麦克风按钮开始录制';
  }

  const canReplace = hasText && editorEnabled;
  ['#replaceButton', '#replaceAllButton'].forEach((selector) => setButtonEnabled(selector, canReplace));
  setButtonEnabled('#diagnoseButton', actionAllowed('run_diagnostic', !status.running) && !commandPending('diagnostic'));
  setButtonEnabled('#historyButton', actionAllowed('view_history', !status.running));
  setButtonEnabled('#settingsButton', actionAllowed('open_settings', !status.running));
  if (status.running && activeDialogRoot === $('#settingsDrawer')) closeSettings();
  if (status.running && activeDialogRoot === $('#historyBackdrop')) closeHistory();
  $('#transcriptEditor').setAttribute('aria-busy', String(Boolean(transcript.streaming || status.running)));

  renderSettings();
}

function renderCurrentSegments(rawSegments) {
  const container = $('#currentSegments');
  const segments = Array.isArray(rawSegments) ? rawSegments.slice(-20) : [];
  container.hidden = !segments.length;
  if (!segments.length) {
    container.replaceChildren();
    return;
  }
  const signature = JSON.stringify(segments);
  if (container.dataset.signature === signature) return;
  container.dataset.signature = signature;
  container.innerHTML = segments.map((segment, index) => {
    const speaker = segment.speaker ?? segment.spk;
    const speakerLabel = speaker !== null && speaker !== undefined && speaker !== ''
      ? String(speaker)
      : segment.timing === 'estimated' ? '估算' : '段落';
    return `<button type="button" data-current-segment="${index}" title="定位到该段文字">
      <span>${escapeHtml(formatClock(segment.start || 0))}</span>
      <strong>${escapeHtml(speakerLabel)}</strong>
      <em>${escapeHtml(segment.text || '')}</em>
    </button>`;
  }).join('');
}

function renderClock(elapsedSeconds) {
  const seconds = Number(elapsedSeconds) || 0;
  const elapsed = formatClock(seconds);
  $('#sessionClock').textContent = elapsed;
  $('#dockClock').textContent = elapsed;
  $('#sessionProgress').style.width = `${Math.min(100, (seconds / (state.capabilities.max_recording_seconds || 7200)) * 100)}%`;
}

function renderAudioLevel(value) {
  const level = Math.max(0, Math.min(1, Number(value) || 0));
  const root = document.documentElement;
  root.style.setProperty('--audio-scale', (0.82 + level * 0.62).toFixed(3));
  root.style.setProperty('--audio-glow', `${Math.round(16 + level * 38)}px`);
  root.style.setProperty('--audio-wave-opacity', (0.2 + level * 0.64).toFixed(3));
  $('#recordDock').setAttribute('data-audio-level', level.toFixed(2));
}

function renderMicrophoneState() {
  const microphone = state.audio.microphone || {};
  const status = microphone.status || 'idle';
  const name = microphone.name || '系统默认麦克风';
  const statusLabels = {
    idle: '麦克风就绪',
    opening: '正在连接麦克风',
    active: '输入正常',
    paused: '输入已暂停',
    error: '麦克风不可用',
  };
  const value = $('#microphoneValue');
  value.textContent = name;
  value.title = microphone.message || `${name} · ${statusLabels[status] || status}`;
  value.dataset.status = status;
  if (!state.transcript.text.trim()) {
    $('#microphoneHint').textContent = microphone.message || `${statusLabels[status] || status} · 所有处理均在本机完成`;
  }
}

function renderSettings() {
  const settings = settingsDraft || state.settings;
  const languageSelect = $('#languageSelect');
  const currentOptions = [...languageSelect.options].map((option) => option.value).join('|');
  const nextOptions = state.options.languages.map((item) => item.value).join('|');
  if (currentOptions !== nextOptions) {
    languageSelect.innerHTML = state.options.languages
      .map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`)
      .join('');
  }
  languageSelect.value = settings.recognition_language;
  $('#hotkeyInput').value = settings.hotkey || '';
  $('#customLexiconPath').textContent = settings.lexicon_path || '未导入';
  const microphoneSelect = $('#microphoneSelect');
  const microphoneSignature = JSON.stringify(state.options.microphones || []);
  if (microphoneSelect.dataset.signature !== microphoneSignature) {
    microphoneSelect.dataset.signature = microphoneSignature;
    microphoneSelect.innerHTML = [
      '<option value="">系统默认麦克风</option>',
      ...(state.options.microphones || []).map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label || item.name || item.id)}</option>`),
    ].join('');
  }
  microphoneSelect.value = settings.microphone_device || '';
  if (!microphoneSelect.value && settings.microphone_device) microphoneSelect.value = '';
  microphoneSelect.disabled = state.status.running;
  $('#microphoneSelectHint').textContent = state.status.running ? '当前任务结束后可切换' : '下次录音开始时生效';

  $$('.setting-toggle').forEach((button) => {
    const on = Boolean(settings[button.dataset.setting]);
    const supported = button.dataset.setting !== 'streaming_transcription_enabled' || state.capabilities.streaming !== false;
    button.classList.toggle('is-on', on);
    button.setAttribute('aria-pressed', String(on));
    button.disabled = !supported;
    button.setAttribute('aria-disabled', String(!supported));
  });
  $('#settingsHint').textContent = settingsDirty ? '有尚未保存的修改' : state.status.running ? '识别相关设置将在下一次录音生效' : '保存后生效';

  const enabled = new Set(settings.enabled_builtin_lexicons || []);
  const lexiconList = $('#lexiconList');
  const signature = JSON.stringify([state.options.lexicons, [...enabled]]);
  if (lexiconList.dataset.signature !== signature) {
    lexiconList.dataset.signature = signature;
    lexiconList.innerHTML = state.options.lexicons.map((item) => `
      <button class="lexicon-option ${enabled.has(item.id) ? 'is-selected' : ''}" type="button" data-lexicon="${escapeHtml(item.id)}" aria-pressed="${String(enabled.has(item.id))}">
        <span class="lexicon-check">${enabled.has(item.id) ? '✓' : ''}</span>
        <span><strong>${escapeHtml(item.title)}</strong><em>${escapeHtml(item.description || '')}</em></span>
      </button>`).join('');
  }
}

function updateState(payload) {
  const next = parsePayload(payload);
  if (!next) return;
  state = {
    ...state,
    ...next,
    status: { ...state.status, ...(next.status || {}) },
    transcript: { ...state.transcript, ...(next.transcript || {}) },
    settings: { ...state.settings, ...(next.settings || {}) },
    options: { ...state.options, ...(next.options || {}) },
    audio: {
      ...state.audio,
      ...(next.audio || {}),
      microphone: { ...state.audio.microphone, ...(next.audio?.microphone || {}) },
    },
    history: Array.isArray(next.history) ? next.history : state.history,
    capabilities: { ...state.capabilities, ...(next.capabilities || {}) },
  };
  settlePendingCommands();
  render();
}

function updateStatus(payload) {
  const status = parsePayload(payload);
  if (status) updateState({ status });
}

function updateClock(elapsedSeconds) {
  const seconds = Number(elapsedSeconds) || 0;
  state = { ...state, status: { ...state.status, elapsed_seconds: seconds } };
  renderClock(seconds);
}

function updateTranscript(payload) {
  const transcript = parsePayload(payload);
  if (!transcript) return;
  cancelPendingTranscript();
  streamingBaseText = '';
  streamingPreviewText = '';
  localTranscriptText = null;
  updateState({ transcript });
}

function updateTranscriptDelta(payload) {
  const delta = parsePayload(payload);
  if (!delta || !Number.isInteger(Number(delta.revision))) return;
  const revision = Number(delta.revision);
  if (delta.op === 'begin') {
    if (revision < Number(state.transcript.revision || 0)) return;
    cancelPendingTranscript();
    streamingBaseText = String(state.transcript.text || '').trim();
    streamingPreviewText = '';
    localTranscriptText = null;
    updateState({ transcript: { ...state.transcript, streaming: true, revision } });
    return;
  }
  if (!['patch', 'commit'].includes(delta.op)) return;
  if (Number(delta.base_revision) !== Number(state.transcript.revision || 0)) return;
  if (!streamingBaseText && typeof delta.base_text === 'string') streamingBaseText = delta.base_text;
  const start = Math.max(0, Math.min(streamingPreviewText.length, Number(delta.start) || 0));
  const deleteCount = Math.max(0, Number(delta.delete_count) || 0);
  streamingPreviewText = `${streamingPreviewText.slice(0, start)}${String(delta.text || '')}${streamingPreviewText.slice(start + deleteCount)}`;
  const text = streamingBaseText && streamingPreviewText
    ? `${streamingBaseText}\n\n${streamingPreviewText}`
    : streamingPreviewText || streamingBaseText;
  updateState({
    transcript: {
      ...state.transcript,
      text,
      streaming: delta.op !== 'commit',
      revision,
      session_id: '',
      segments: [],
    },
  });
  if (delta.op === 'commit') {
    streamingBaseText = '';
    streamingPreviewText = '';
  }
}

function updateSettings(payload) {
  const settingsPatch = parsePayload(payload);
  if (settingsPatch) {
    updateState(settingsPatch);
    if (settingsDraft && !settingsDirty) settingsDraft = structuredClone(state.settings);
  }
}

function updateAudioLevel(level) {
  const normalized = Math.max(0, Math.min(1, Number(level) || 0));
  state = { ...state, audio: { ...state.audio, level: normalized } };
  renderAudioLevel(normalized);
}

function updateMicrophoneState(payload) {
  const microphone = parsePayload(payload);
  if (!microphone) return;
  state = { ...state, audio: { ...state.audio, microphone: { ...state.audio.microphone, ...microphone } } };
  renderMicrophoneState();
}

function updateHistory(payload) {
  const history = parsePayload(payload);
  if (!Array.isArray(history)) return;
  state = { ...state, history };
  for (const sessionId of historyDetails.keys()) {
    if (!history.some((item) => item.id === sessionId)) historyDetails.delete(sessionId);
  }
  $('#historyCount').textContent = String(history.length);
  if (!$('#historyBackdrop').hidden) renderHistory();
}

function updateHistorySession(payload) {
  const session = parsePayload(payload);
  if (!session?.id) return;
  historyDetails.set(session.id, session);
  if (requestedHistoryId === session.id) requestedHistoryId = '';
  if (!$('#historyBackdrop').hidden && selectedHistoryId === session.id) renderHistory();
}

function formatHistoryTime(value, duration = null) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return '--:--';
  return formatClock(seconds);
}

function historySourceLabel(source) {
  if (source?.kind === 'audio_file') return source.name || '导入音频';
  return '麦克风录音';
}

function renderHistory() {
  const list = $('#historyList');
  if (!state.history.length) {
    selectedHistoryId = '';
    list.innerHTML = '<div class="history-empty">还没有已完成的会话</div>';
    $('#historyDetail').innerHTML = '<div class="history-empty-detail">完成一次录音或音频识别后，时间段和段落会保存在这里。</div>';
    $('#loadHistoryButton').disabled = true;
    return;
  }
  if (!state.history.some((session) => session.id === selectedHistoryId)) selectedHistoryId = state.history[0].id;
  list.innerHTML = state.history.map((session) => {
    const date = new Date(session.created_at);
    const dateLabel = Number.isNaN(date.getTime()) ? session.created_at : date.toLocaleString('zh-CN', { hour12: false });
    return `<button class="history-item ${session.id === selectedHistoryId ? 'is-selected' : ''}" type="button" data-session-id="${escapeHtml(session.id)}" aria-current="${session.id === selectedHistoryId ? 'true' : 'false'}">
      <strong>${escapeHtml(historySourceLabel(session.source))}</strong>
      <span>${escapeHtml(dateLabel)} · ${escapeHtml(formatClock(session.duration || 0))}</span>
      <em>${escapeHtml((session.text_preview || session.text || '').slice(0, 58))}</em>
    </button>`;
  }).join('');

  const summary = state.history.find((item) => item.id === selectedHistoryId);
  const session = historyDetails.get(selectedHistoryId) || (Array.isArray(summary?.segments) ? summary : null);
  if (!session) {
    $('#historyDetail').innerHTML = '<div class="history-empty-detail">正在加载会话详情…</div>';
    $('#loadHistoryButton').disabled = true;
    requestHistoryDetail(selectedHistoryId);
    return;
  }
  const segments = Array.isArray(session?.segments) ? session.segments : [];
  const hasSpeaker = (value) => value !== null && value !== undefined && value !== '';
  const speakerCount = new Set(segments.map((segment) => segment.speaker ?? segment.spk).filter(hasSpeaker)).size;
  const segmentRows = segments.length ? segments.map((segment) => {
    const speaker = segment.speaker ?? segment.spk;
    const timing = segment.timing === 'estimated' ? '估算' : '时间';
    return `<div class="history-segment">
      <span>${escapeHtml(formatHistoryTime(segment.start, session.duration))} - ${escapeHtml(formatHistoryTime(segment.end, session.duration))}</span>
      <strong>${hasSpeaker(speaker) ? escapeHtml(String(speaker)) : timing}</strong>
      <p>${escapeHtml(segment.text || '')}</p>
    </div>`;
  }).join('') : '<div class="history-empty-detail">此会话没有可用的时间段信息。</div>';
  $('#historyDetail').innerHTML = `
    <header><div><span>${escapeHtml(historySourceLabel(session.source))}</span><strong>${escapeHtml(formatClock(session.duration || 0))}</strong></div><em>${segments.length} 段${speakerCount ? ` · ${speakerCount} 位说话人` : ''}</em></header>
    <div class="history-segments">${segmentRows}</div>`;
  $('#loadHistoryButton').disabled = !actionAllowed('load_history', !state.status.running) || commandPending('load-history');
}

function requestHistoryDetail(sessionId) {
  if (!sessionId || historyDetails.has(sessionId) || requestedHistoryId === sessionId) return;
  requestedHistoryId = sessionId;
  if (bridge && typeof bridge.requestSession === 'function') {
    bridge.requestSession(sessionId);
    return;
  }
  const legacy = state.history.find((item) => item.id === sessionId);
  if (legacy) historyDetails.set(sessionId, legacy);
  requestedHistoryId = '';
}

function openHistory() {
  renderHistory();
  $('#historyBackdrop').hidden = false;
  activateDialog($('#historyBackdrop'), $('#closeHistoryButton'));
}

function closeHistory() {
  $('#historyBackdrop').hidden = true;
  deactivateDialog($('#historyBackdrop'));
}

function handleNotification(payload) {
  const notice = parsePayload(payload);
  if (!notice) return;
  if (notice.kind === 'toast') {
    showToast(notice.message || notice.title || '完成');
    return;
  }
  $('#modalKicker').textContent = notice.kind === 'error' ? 'RUNTIME ERROR' : 'NOTICE';
  $('#modalTitle').textContent = notice.title || (notice.kind === 'error' ? '出现问题' : '提示');
  $('#modalMessage').textContent = notice.message || '';
  $('#modalMark').textContent = notice.kind === 'error' ? '!' : 'i';
  $('#modalMark').classList.toggle('is-error', notice.kind === 'error');
  $('#modalCancel').hidden = true;
  $('#modalConfirm').textContent = '知道了';
  confirmationResolve = null;
  $('#modalBackdrop').hidden = false;
  activateDialog($('#modalBackdrop'), $('#modalConfirm'));
}

function openSettings(section = '') {
  settingsDraft = structuredClone(state.settings);
  settingsDirty = false;
  $('#settingsDrawer').hidden = false;
  $('#drawerBackdrop').hidden = false;
  requestAnimationFrame(() => document.body.classList.add('settings-open'));
  activateDialog($('#settingsDrawer'), $('#closeSettingsButton'));
  if (section === 'lexicon') {
    $('.lexicon-list')?.scrollIntoView({ block: 'center' });
  }
}

function closeSettings() {
  settingsDraft = null;
  settingsDirty = false;
  document.body.classList.remove('settings-open');
  window.setTimeout(() => {
    $('#settingsDrawer').hidden = true;
    $('#drawerBackdrop').hidden = true;
    deactivateDialog($('#settingsDrawer'));
  }, 260);
}

function focusableElements(root) {
  return $$('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')
    .filter((element) => root.contains(element) && !element.hidden && element.getClientRects().length > 0);
}

function setWorkspaceInert(inert) {
  ['#titlebar', '.runtime-shell', '#recordDock'].forEach((selector) => {
    const element = $(selector);
    if (element) element.inert = inert;
  });
}

function activateDialog(root, preferredFocus) {
  dialogReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  activeDialogRoot = root;
  setWorkspaceInert(true);
  requestAnimationFrame(() => (preferredFocus || focusableElements(root)[0] || root).focus());
}

function deactivateDialog(root) {
  if (activeDialogRoot !== root) return;
  activeDialogRoot = null;
  setWorkspaceInert(false);
  const returnFocus = dialogReturnFocus;
  dialogReturnFocus = null;
  if (returnFocus?.isConnected) returnFocus.focus();
}

function handleDialogKeydown(event) {
  if (!activeDialogRoot) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    if (activeDialogRoot === $('#settingsDrawer')) closeSettings();
    else if (activeDialogRoot === $('#historyBackdrop')) closeHistory();
    else if (activeDialogRoot === $('#modalBackdrop')) closeModal(false);
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = focusableElements(activeDialogRoot);
  if (!focusable.length) {
    event.preventDefault();
    activeDialogRoot.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function closeModal(result = true) {
  $('#modalBackdrop').hidden = true;
  deactivateDialog($('#modalBackdrop'));
  const resolve = confirmationResolve;
  confirmationResolve = null;
  $('#modalCancel').hidden = true;
  $('#modalConfirm').textContent = '知道了';
  if (resolve) resolve(Boolean(result));
}

function confirmAction(title, message, confirmLabel) {
  if (confirmationResolve) confirmationResolve(false);
  $('#modalKicker').textContent = 'CONFIRM';
  $('#modalTitle').textContent = title;
  $('#modalMessage').textContent = message;
  $('#modalMark').textContent = '!';
  $('#modalMark').classList.remove('is-error');
  $('#modalCancel').hidden = false;
  $('#modalConfirm').textContent = confirmLabel;
  $('#modalBackdrop').hidden = false;
  activateDialog($('#modalBackdrop'), $('#modalCancel'));
  return new Promise((resolve) => { confirmationResolve = resolve; });
}

function collectSettingsDraft() {
  const patch = {
    recognition_language: $('#languageSelect').value,
    microphone_device: $('#microphoneSelect').value,
    hotkey: $('#hotkeyInput').value,
  };
  $$('.setting-toggle').forEach((button) => {
    patch[button.dataset.setting] = button.classList.contains('is-on');
  });
  patch.enabled_builtin_lexicons = $$('.lexicon-option.is-selected').map((button) => button.dataset.lexicon);
  return patch;
}

function markSettingsDirty() {
  settingsDraft = { ...(settingsDraft || state.settings), ...collectSettingsDraft() };
  settingsDirty = true;
  $('#settingsHint').textContent = '有尚未保存的修改';
}

function saveSettings() {
  const patch = collectSettingsDraft();
  settingsDraft = { ...(settingsDraft || state.settings), ...patch };
  settingsDirty = false;
  callBridge('saveSettings', JSON.stringify(patch));
  if (!state.connected) {
    updateState({ settings: { ...state.settings, ...patch } });
    showToast('设置已保存');
  }
  $('#settingsHint').textContent = '设置已提交';
}

function sendTranscriptNow() {
  window.clearTimeout(transcriptTimer);
  transcriptTimer = undefined;
  const text = $('#transcriptEditor').value;
  const baseRevision = Number(state.transcript.revision || 0);
  const revision = baseRevision + 1;
  lastRenderedText = text;
  localTranscriptText = text;
  if (bridge && typeof bridge.setTranscriptRevision === 'function') {
    bridge.setTranscriptRevision(JSON.stringify({ base_revision: baseRevision, revision, text }));
  } else {
    callBridge('setTranscript', text);
  }
  render();
}

function flushPendingTranscript() {
  if (transcriptTimer) sendTranscriptNow();
}

function cancelPendingTranscript() {
  window.clearTimeout(transcriptTimer);
  transcriptTimer = undefined;
}

function setExportMenu(open) {
  const menu = $('#exportMenu');
  const button = $('#exportButton');
  menu.hidden = !open;
  button.setAttribute('aria-expanded', String(open));
}

function findNext() {
  const editor = $('#transcriptEditor');
  const query = $('#findInput').value;
  if (!query) return;
  const start = editor.value.indexOf(query, Math.max(editor.selectionEnd, 0));
  const wrapped = start < 0 ? editor.value.indexOf(query) : start;
  if (wrapped < 0) {
    showToast('没有找到匹配内容');
    return;
  }
  editor.focus();
  editor.setSelectionRange(wrapped, wrapped + query.length);
}

function replaceCurrent() {
  const editor = $('#transcriptEditor');
  if (editor.readOnly) return;
  const query = $('#findInput').value;
  const replacement = $('#replaceInput').value;
  if (!query) return;
  if (editor.value.slice(editor.selectionStart, editor.selectionEnd) !== query) findNext();
  if (editor.value.slice(editor.selectionStart, editor.selectionEnd) !== query) return;
  editor.setRangeText(replacement, editor.selectionStart, editor.selectionEnd, 'end');
  sendTranscriptNow();
}

function replaceAll() {
  const editor = $('#transcriptEditor');
  if (editor.readOnly) return;
  const query = $('#findInput').value;
  if (!query) return;
  const count = editor.value.split(query).length - 1;
  if (!count) {
    showToast('没有找到匹配内容');
    return;
  }
  editor.value = editor.value.split(query).join($('#replaceInput').value);
  sendTranscriptNow();
  showToast(`已替换 ${count} 处`);
}

function captureHotkey(event) {
  event.preventDefault();
  if (event.key === 'Escape') {
    event.currentTarget.blur();
    return;
  }
  if (['Control', 'Shift', 'Alt', 'Meta'].includes(event.key)) return;
  const parts = [];
  if (event.ctrlKey) parts.push('Ctrl');
  if (event.altKey) parts.push('Alt');
  if (event.shiftKey) parts.push('Shift');
  if (event.metaKey) parts.push('Meta');
  let key = event.key.length === 1 ? event.key.toUpperCase() : event.key;
  if (key === ' ') key = 'Space';
  parts.push(key);
  event.currentTarget.value = parts.join('+');
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function bindEvents() {
  $('#recordButton').addEventListener('click', () => {
    flushPendingTranscript();
    if (state.status.recording) runCommand('primary', 'stopRecording');
    else if (state.status.running && actionAllowed('cancel_cycle', true)) {
      runCommand('primary', bridge && typeof bridge.cancelRecognition === 'function' ? 'cancelRecognition' : 'stopRecording');
    } else if (!state.status.running) runCommand('primary', 'startRecording');
  });
  $('#pauseButton').addEventListener('click', () => {
    const method = state.status.paused && bridge && typeof bridge.resumeRecording === 'function'
      ? 'resumeRecording'
      : !state.status.paused && bridge && typeof bridge.pauseRecording === 'function'
        ? 'pauseRecording'
        : 'togglePause';
    runCommand('pause', method);
  });
  $('#collapseSidebarButton').addEventListener('click', () => {
    if (window.matchMedia('(max-width: 900px)').matches) {
      setCompactPanel(false);
      return;
    }
    setSidebarCollapsed(!document.body.classList.contains('sidebar-collapsed'));
  });
  $('#compactPanelButton').addEventListener('click', () => setCompactPanel(!document.body.classList.contains('compact-panel-open')));
  $('#historyButton').addEventListener('click', openHistory);
  $('#closeHistoryButton').addEventListener('click', closeHistory);
  $('#historyBackdrop').addEventListener('click', (event) => {
    if (event.target === $('#historyBackdrop')) closeHistory();
  });
  $('#historyList').addEventListener('click', (event) => {
    const item = event.target.closest('[data-session-id]');
    if (!item) return;
    selectedHistoryId = item.dataset.sessionId;
    renderHistory();
    requestHistoryDetail(selectedHistoryId);
  });
  $('#loadHistoryButton').addEventListener('click', async () => {
    if (!selectedHistoryId) return;
    if (state.status.running || !actionAllowed('load_history', !state.status.running)) return;
    if (state.transcript.dirty || transcriptTimer || localTranscriptText !== null) {
      const confirmed = await confirmAction('载入历史转写稿', '当前修改尚未完成保存，继续会覆盖工作区内容。', '继续载入');
      if (!confirmed) return;
    }
    cancelPendingTranscript();
    runCommand('load-history', 'loadSession', selectedHistoryId);
    closeHistory();
  });
  ['#importButton', '#importSideButton'].forEach((selector) => $(selector).addEventListener('click', () => {
    flushPendingTranscript();
    runCommand('import', 'importAudio');
  }));
  ['#copyButton', '#copyDockButton'].forEach((selector) => $(selector).addEventListener('click', () => {
    flushPendingTranscript();
    callBridge('copyTranscript');
    showToast('转写稿已复制');
  }));
  $('#clearButton').addEventListener('click', async () => {
    if (!$('#transcriptEditor').value.trim()) return;
    const confirmed = await confirmAction('清空转写稿', '当前转写内容将从工作区移除。', '清空');
    if (!confirmed) return;
    cancelPendingTranscript();
    localTranscriptText = null;
    runCommand('clear', 'clearTranscript');
    if (!state.connected) updateState({ transcript: { text: '' } });
  });
  $('#settingsButton').addEventListener('click', () => openSettings());
  $('#languageTile').addEventListener('click', () => openSettings());
  $('#lexiconTile').addEventListener('click', () => openSettings('lexicon'));
  $('#microphoneTile').addEventListener('click', () => openSettings());
  $('#closeSettingsButton').addEventListener('click', closeSettings);
  $('#cancelSettingsButton').addEventListener('click', closeSettings);
  $('#drawerBackdrop').addEventListener('click', closeSettings);
  $('#saveSettingsButton').addEventListener('click', saveSettings);
  $('#selectLexiconButton').addEventListener('click', () => callBridge('selectCustomLexicon'));
  $('#helpButton').addEventListener('click', () => callBridge('openHelp'));
  $('#diagnoseButton').addEventListener('click', () => runCommand('diagnostic', 'openHealth'));
  $('#logButton').addEventListener('click', () => callBridge('openLog'));
  $('#minimizeButton').addEventListener('click', () => callBridge('minimizeWindow'));
  $('#maximizeButton').addEventListener('click', () => callBridge('toggleMaximize'));
  $('#closeButton').addEventListener('click', () => callBridge('closeWindow'));
  $('#titlebar').addEventListener('pointerdown', (event) => {
    if (!event.target.closest('button')) callBridge('startWindowMove');
  });
  $('#titlebar').addEventListener('dblclick', (event) => {
    if (!event.target.closest('button')) callBridge('toggleMaximize');
  });

  $('#exportButton').addEventListener('click', (event) => {
    event.stopPropagation();
    setExportMenu($('#exportMenu').hidden);
  });
  document.addEventListener('click', () => setExportMenu(false));
  $$('#exportMenu [data-export]').forEach((button) => button.addEventListener('click', () => {
    flushPendingTranscript();
    callBridge('exportDocument', button.dataset.export);
  }));

  $('#findButton').addEventListener('click', () => {
    $('#findStrip').hidden = false;
    $('#findInput').focus();
  });
  $('#closeFindButton').addEventListener('click', () => { $('#findStrip').hidden = true; });
  $('#findNextButton').addEventListener('click', findNext);
  $('#replaceButton').addEventListener('click', replaceCurrent);
  $('#replaceAllButton').addEventListener('click', replaceAll);
  $('#findInput').addEventListener('keydown', (event) => { if (event.key === 'Enter') findNext(); });

  $('#transcriptEditor').addEventListener('input', () => {
    localTranscriptText = $('#transcriptEditor').value;
    lastRenderedText = localTranscriptText;
    window.clearTimeout(transcriptTimer);
    transcriptTimer = window.setTimeout(sendTranscriptNow, 180);
  });
  $('#currentSegments').addEventListener('click', (event) => {
    const button = event.target.closest('[data-current-segment]');
    if (!button) return;
    const visibleSegments = Array.isArray(state.transcript.segments) ? state.transcript.segments.slice(-20) : [];
    const segment = visibleSegments[Number(button.dataset.currentSegment)];
    const editor = $('#transcriptEditor');
    const segmentText = String(segment?.text || '');
    const start = segmentText ? editor.value.indexOf(segmentText) : -1;
    editor.focus();
    if (start >= 0) editor.setSelectionRange(start, start + segmentText.length);
  });

  $$('.setting-toggle').forEach((button) => button.addEventListener('click', () => {
    button.classList.toggle('is-on');
    button.setAttribute('aria-pressed', String(button.classList.contains('is-on')));
    markSettingsDirty();
  }));
  $('#lexiconList').addEventListener('click', (event) => {
    const option = event.target.closest('.lexicon-option');
    if (!option) return;
    option.classList.toggle('is-selected');
    option.setAttribute('aria-pressed', String(option.classList.contains('is-selected')));
    option.querySelector('.lexicon-check').textContent = option.classList.contains('is-selected') ? '✓' : '';
    markSettingsDirty();
  });
  ['#languageSelect', '#microphoneSelect'].forEach((selector) => $(selector).addEventListener('change', markSettingsDirty));
  $('#hotkeyInput').addEventListener('keydown', (event) => { captureHotkey(event); markSettingsDirty(); });
  $('#modalConfirm').addEventListener('click', () => closeModal(true));
  $('#modalCancel').addEventListener('click', () => closeModal(false));
  $('#modalBackdrop').addEventListener('click', (event) => {
    if (event.target === $('#modalBackdrop')) closeModal(false);
  });
  document.addEventListener('keydown', handleDialogKeydown);
}

function connectRuntime() {
  if (window.qt?.webChannelTransport && window.QWebChannel) {
    new window.QWebChannel(window.qt.webChannelTransport, (channel) => {
      bridge = channel.objects.voiceBridge;
      if (!bridge) return;
      bridge.stateChanged.connect(updateState);
      bridge.notificationRequested.connect(handleNotification);
      const supportsIncrementalUpdates = typeof bridge.readyIncremental === 'function'
        && typeof bridge.statusChanged?.connect === 'function'
        && typeof bridge.clockChanged?.connect === 'function'
        && typeof bridge.transcriptChanged?.connect === 'function'
        && typeof bridge.transcriptDeltaChanged?.connect === 'function'
        && typeof bridge.settingsChanged?.connect === 'function';
      if (supportsIncrementalUpdates) {
        bridge.statusChanged.connect(updateStatus);
        bridge.clockChanged.connect(updateClock);
        bridge.transcriptChanged.connect(updateTranscript);
        if (typeof bridge.transcriptDeltaChanged?.connect === 'function') bridge.transcriptDeltaChanged.connect(updateTranscriptDelta);
        bridge.settingsChanged.connect(updateSettings);
        if (typeof bridge.audioLevelChanged?.connect === 'function') bridge.audioLevelChanged.connect(updateAudioLevel);
        if (typeof bridge.microphoneStateChanged?.connect === 'function') bridge.microphoneStateChanged.connect(updateMicrophoneState);
        if (typeof bridge.historyChanged?.connect === 'function') bridge.historyChanged.connect(updateHistory);
        if (typeof bridge.historySessionChanged?.connect === 'function') bridge.historySessionChanged.connect(updateHistorySession);
        bridge.readyIncremental();
      } else {
        bridge.ready();
      }
    });
    return;
  }

  const mockLexicons = [
    { id: 'government_digital', title: '政务 / 数字政府', description: '政务服务、数字政府等常用表达' },
    { id: 'official_document', title: '公文规范', description: '公文要素、文种和格式术语' },
    { id: 'office_meeting', title: '办公会议', description: '会议纪要、议题和项目协作词汇' },
  ];
  updateState({
    connected: false,
    status: { message: '准备就绪', detail: '浏览器预览 · 实际运行时由 Python 驱动' },
    options: { lexicons: mockLexicons },
    settings: { enabled_builtin_lexicons: mockLexicons.map((item) => item.id) },
    history: [{
      id: 'preview-session',
      created_at: new Date().toISOString(),
      source: { kind: 'microphone' },
      text: '项目例会确认了交付节点。\n\n下一步由产品组整理需求清单。',
      duration: 84,
      paragraphs: ['项目例会确认了交付节点。', '下一步由产品组整理需求清单。'],
      segments: [
        { start: 0, end: 36, text: '项目例会确认了交付节点。', timing: 'estimated' },
        { start: 36, end: 84, text: '下一步由产品组整理需求清单。', timing: 'estimated' },
      ],
    }],
  });
  bridge = {
    startRecording() {
      updateState({
        status: { running: true, recording: true, elapsed_seconds: 0, phase: 'recording', message: '正在实时转写', detail: '麦克风输入正常' },
        audio: { microphone: { status: 'active', name: '系统默认麦克风', message: '' } },
      });
      window.clearInterval(mockTimer);
      mockTimer = window.setInterval(() => {
        updateState({ status: { elapsed_seconds: state.status.elapsed_seconds + 1 } });
        updateAudioLevel(0.18 + Math.random() * 0.72);
      }, 1000);
    },
    stopRecording() {
      window.clearInterval(mockTimer);
      updateAudioLevel(0);
      updateState({
        status: { running: false, recording: false, paused: false, phase: 'ready', message: '转写完成', detail: '结果已保存在当前会话' },
        audio: { microphone: { status: 'idle' } },
      });
    },
    togglePause() { updateState({ status: { paused: !state.status.paused, phase: state.status.paused ? 'recording' : 'paused' } }); },
    clearTranscript() { updateState({ transcript: { text: '' } }); },
    setTranscript(text) { state.transcript.text = text; },
    setTranscriptRevision(payload) {
      const request = parsePayload(payload);
      if (request) updateState({ transcript: { text: request.text || '', revision: request.revision || state.transcript.revision } });
    },
    requestSession(sessionId) {
      const session = state.history.find((item) => item.id === sessionId);
      if (session) updateHistorySession(session);
    },
    loadSession(sessionId) {
      const session = historyDetails.get(sessionId) || state.history.find((item) => item.id === sessionId);
      if (session) updateTranscript({ ...session, editable: state.settings.transcript_editable, streaming: false, session_id: session.id, revision: state.transcript.revision + 1 });
    },
    saveSettings(payload) { updateState({ settings: { ...state.settings, ...parsePayload(payload) } }); },
    copyTranscript() { navigator.clipboard?.writeText(state.transcript.text); },
    importAudio() { showToast('运行时中会打开系统文件选择器'); },
    exportDocument(kind) { showToast(`准备导出：${kind}`); },
    selectCustomLexicon() { showToast('运行时中会打开词库文件选择器'); },
    openHelp() { handleNotification({ kind: 'info', title: '使用帮助', message: '点击底部麦克风按钮开始录制，录音内容会实时写入转写稿。' }); },
    openHealth() { showToast('正在生成系统诊断'); },
    openLog() { showToast('运行时中会打开日志文件'); },
  };
}

bindEvents();
setSidebarCollapsed(readSidebarPreference(), false);
render();
connectRuntime();
requestAnimationFrame(() => enhanceGlass(document));
