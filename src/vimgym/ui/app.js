// vimgym v0.2 — provider-neutral, dependency-free vault browser.
// Provider content is always rendered through textContent. The server never
// supplies trusted markup and this client intentionally has no HTML parser.

'use strict';

const SESSION_PAGE_SIZE = 50;
const MESSAGE_PAGE_SIZE = 100;
const MAX_MESSAGE_PAGES = 3;
const MAX_MOUNTED_MESSAGES = MESSAGE_PAGE_SIZE * MAX_MESSAGE_PAGES;
const MAX_PREVIEW_CHARS = 8192;

const State = {
  sessions: [],
  sessionTotal: 0,
  sessionCursor: null,
  loadingSessions: false,
  activeSession: null,
  activeSessionId: null,
  detailRequest: 0,
  messages: [],
  messageCursor: null,
  messagePages: 0,
  loadingMessages: false,
  transcriptQuery: '',
  filters: { provider: '', kind: '', lifecycle: '' },
  stats: null,
  sources: [],
  searchOpen: false,
  searchResults: [],
  searchCursor: null,
  searchSelected: 0,
  searchTimer: null,
  searchAbort: null,
  returnFocus: null,
  mode: 'NORMAL',
  socket: null,
  reconnectTimer: null,
  reconnectAttempt: 0,
};

class ApiError extends Error {
  constructor(status, message) {
    super(message || `Request failed (${status})`);
    this.status = status;
  }
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { Accept: 'application/json', ...(options.headers || {}) },
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      message = body.detail || body.error || message;
    } catch (_error) {
      // A non-JSON error response still gets a bounded, status-only message.
    }
    throw new ApiError(response.status, message);
  }
  return response.json();
}

async function firstAvailable(urls) {
  for (const url of urls) {
    try {
      return await apiFetch(url);
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) throw error;
    }
  }
  return null;
}

function queryString(values) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== '' && value !== null && value !== undefined) query.set(key, String(value));
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : '';
}

function element(tag, attributes = {}, children = []) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === null || value === undefined || value === false) continue;
    if (name === 'className') node.className = value;
    else if (name === 'text') node.textContent = String(value);
    else if (name === 'hidden') node.hidden = Boolean(value);
    else if (name === 'disabled') node.disabled = Boolean(value);
    else if (name === 'checked') node.checked = Boolean(value);
    else if (name === 'value') node.value = String(value);
    else node.setAttribute(name, value === true ? '' : String(value));
  }
  const childList = Array.isArray(children) ? children : [children];
  for (const child of childList.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) {
  node.replaceChildren();
  return node;
}

function announce(message) {
  const region = document.getElementById('announcer');
  region.textContent = '';
  window.setTimeout(() => { region.textContent = message; }, 20);
}

function showToast(message) {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.classList.add('show');
  window.clearTimeout(toast.dismissTimer);
  toast.dismissTimer = window.setTimeout(() => toast.classList.remove('show'), 3500);
}

function sessionId(session) {
  return String(session.id || session.session_id || session.session_uuid || session.internal_id || '');
}

function workspaceLabel(session) {
  if (typeof session.workspace === 'string') return session.workspace;
  if (session.workspace && typeof session.workspace === 'object') {
    return session.workspace.name || session.workspace.display_name || session.workspace.cwd || '';
  }
  return session.workspace_name || session.project_name || '';
}

function normalizeSession(raw) {
  const provider = raw.provider || raw.source_provider || 'claude_code';
  const kind = raw.kind || raw.session_type || 'unknown';
  const lifecycle = raw.lifecycle || (raw.archived ? 'archived' : 'active');
  return {
    ...raw,
    id: sessionId(raw),
    provider,
    kind,
    lifecycle,
    title: raw.title || raw.ai_title || raw.display_title || '(untitled session)',
    workspaceLabel: workspaceLabel(raw),
    branch: raw.branch || raw.git_branch || '',
    startedAt: raw.started_at || raw.created_at || raw.first_seen_at || '',
    updatedAt: raw.updated_at || raw.ended_at || raw.last_seen_at || '',
    health: raw.health || raw.parser_health || raw.status || 'healthy',
    messageCount: Number(raw.message_count || raw.messages_count || 0),
    tools: Array.isArray(raw.tools) ? raw.tools : (Array.isArray(raw.tools_used) ? raw.tools_used : []),
    originator: raw.originator || raw.client_name || raw.client || '',
    model: raw.model || raw.model_name || '',
    parentId: raw.parent_session_id || raw.parent_id || '',
    rootId: raw.root_session_id || raw.root_id || '',
  };
}

function normalizeEnvelope(body, legacyKey) {
  const rawItems = Array.isArray(body.items)
    ? body.items
    : (Array.isArray(body[legacyKey]) ? body[legacyKey] : []);
  return {
    items: rawItems,
    nextCursor: body.next_cursor || null,
    total: Number.isFinite(body.total) ? body.total : null,
  };
}

function normalizeMessage(raw, index) {
  const blocks = Array.isArray(raw.blocks)
    ? raw.blocks
    : (Array.isArray(raw.message_blocks) ? raw.message_blocks : (Array.isArray(raw.content) ? raw.content : []));
  return {
    ...raw,
    id: String(raw.id || raw.message_id || `message-${raw.sequence ?? index}`),
    sequence: Number.isFinite(raw.sequence) ? raw.sequence : index,
    role: raw.role || 'unknown',
    turn: raw.turn ?? raw.turn_id,
    timestamp: raw.timestamp || raw.created_at || '',
    blocks,
  };
}

function providerName(provider) {
  const names = { claude_code: 'Claude Code', claude: 'Claude Code', codex: 'Codex' };
  return names[provider] || provider || 'Unknown provider';
}

function providerClass(provider) {
  return provider === 'codex' ? 'provider-codex' : 'provider-claude';
}

function relativeTime(value) {
  if (!value) return '';
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return '';
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}

function safeStringify(value) {
  if (typeof value === 'string') return value;
  try { return JSON.stringify(value, null, 2); } catch (_error) { return '[unavailable structured data]'; }
}

function badge(text, variant = '') {
  return element('span', { className: `badge ${variant}`.trim(), text });
}

function setBusy(node, busy) {
  node.setAttribute('aria-busy', String(Boolean(busy)));
}

function renderMatrix() {
  const canvas = document.getElementById('matrix-canvas');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!canvas || reduceMotion || !canvas.getContext) return;
  const context = canvas.getContext('2d');
  if (!context) return;
  const glyphs = 'アイウエオカキクケコ0123456789ABCDEF<>[]{}';
  let drops = [];
  let lastFrame = 0;

  function resize() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    drops = Array.from({ length: Math.ceil(canvas.width / 18) }, () => Math.random() * -60);
  }

  function frame(time) {
    if (time - lastFrame > 70 && !document.hidden) {
      context.fillStyle = 'rgba(6, 6, 8, 0.08)';
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.fillStyle = '#00ff41';
      context.font = '12px monospace';
      drops.forEach((drop, index) => {
        context.globalAlpha = 0.15 + Math.random() * 0.45;
        context.fillText(glyphs[Math.floor(Math.random() * glyphs.length)], index * 18, drop * 18);
        drops[index] = drop * 18 > canvas.height && Math.random() > 0.97 ? 0 : drop + 0.35;
      });
      context.globalAlpha = 1;
      lastFrame = time;
    }
    window.requestAnimationFrame(frame);
  }

  resize();
  window.addEventListener('resize', resize, { passive: true });
  window.requestAnimationFrame(frame);
}

async function loadAuxiliaryData() {
  try {
    const [stats, sourceBody] = await Promise.all([
      firstAvailable(['/api/stats']),
      firstAvailable(['/api/sources', '/api/config/sources']),
    ]);
    State.stats = stats;
    State.sources = Array.isArray(sourceBody)
      ? sourceBody
      : sourceBody && Array.isArray(sourceBody.sources)
      ? sourceBody.sources
      : (Array.isArray(sourceBody?.items) ? sourceBody.items : []);
    renderStats();
    renderSourceHealth();
    renderTools();
    updateStatusbar();
  } catch (error) {
    renderSourceHealth(error.message);
  }
}

function renderStats() {
  const root = clear(document.getElementById('sidebarStats'));
  const stats = State.stats || {};
  const rows = [
    ['sessions', stats.total_sessions ?? State.sessionTotal],
    ['messages', stats.total_messages ?? '—'],
    ['vault size', stats.db_size_bytes === undefined ? '—' : formatBytes(stats.db_size_bytes)],
    ['degraded', stats.degraded_sessions ?? stats.degraded_artifacts ?? '—'],
  ];
  for (const [term, value] of rows) {
    root.append(element('dt', { text: term }), element('dd', { text: value }));
  }
}

function renderTools() {
  const root = clear(document.getElementById('sidebarTools'));
  const topTools = Array.isArray(State.stats?.top_tools) ? State.stats.top_tools : [];
  if (!topTools.length) {
    root.append(element('span', { className: 'muted', text: 'No tool activity yet' }));
    return;
  }
  for (const item of topTools.slice(0, 16)) {
    const name = typeof item === 'string' ? item : item.tool;
    const count = typeof item === 'object' ? item.count : null;
    root.append(element('span', {
      className: 'tool-chip',
      text: name,
      title: count === null ? name : `${count} uses`,
    }));
  }
}

function renderSourceHealth(errorMessage = '') {
  const root = clear(document.getElementById('sourceHealth'));
  if (errorMessage) {
    root.append(element('p', { className: 'inline-error', text: `Health unavailable: ${errorMessage}` }));
    return;
  }
  if (!State.sources.length) {
    root.append(element('p', { className: 'muted', text: 'No configured sources reported' }));
    return;
  }
  for (const source of State.sources) {
    const status = source.health || source.status
      || (source.enabled === false ? 'disabled'
        : (source.exists === false ? 'missing'
          : (source.parser_available === false ? 'degraded' : 'healthy')));
    const row = element('div', { className: 'source-row' });
    const provider = source.provider || source.type || source.id || 'source';
    row.append(
      element('span', { className: 'source-name', text: providerName(provider) }),
      badge(status, `health-${status}`),
    );
    if (source.diagnostic || source.message) {
      row.append(element('span', { className: 'source-diagnostic', text: source.diagnostic || source.message }));
    }
    root.append(row);
  }
}

function readFilters() {
  State.filters = {
    provider: document.getElementById('providerFilter').value,
    kind: document.getElementById('kindFilter').value,
    lifecycle: document.getElementById('lifecycleFilter').value,
  };
}

async function loadSessions({ append = false } = {}) {
  if (State.loadingSessions) return;
  State.loadingSessions = true;
  const list = document.getElementById('inboxList');
  const alert = document.getElementById('inboxAlert');
  const loadMore = document.getElementById('loadMoreSessions');
  setBusy(list, true);
  loadMore.disabled = true;
  alert.hidden = true;

  if (!append) {
    State.sessionCursor = null;
    State.sessions = [];
    renderSessionSkeletons();
  }

  try {
    const body = await apiFetch('/api/sessions' + queryString({
      ...State.filters,
      limit: SESSION_PAGE_SIZE,
      cursor: append ? State.sessionCursor : null,
    }));
    const envelope = normalizeEnvelope(body, 'sessions');
    const normalized = envelope.items.map(normalizeSession).filter(session => session.id);
    State.sessions = append ? State.sessions.concat(normalized) : normalized;
    State.sessionCursor = envelope.nextCursor;
    State.sessionTotal = envelope.total ?? State.sessions.length;
    renderSessions();
    loadMore.hidden = !State.sessionCursor;
    document.getElementById('inboxCount').textContent = envelope.total === null
      ? `${State.sessions.length}${State.sessionCursor ? '+' : ''}`
      : String(envelope.total);
  } catch (error) {
    if (!append) clear(list);
    alert.textContent = `Could not load sessions: ${error.message}`;
    alert.hidden = false;
    loadMore.hidden = true;
  } finally {
    State.loadingSessions = false;
    setBusy(list, false);
    loadMore.disabled = false;
    renderStats();
    updateStatusbar();
  }
}

function renderSessionSkeletons() {
  const root = clear(document.getElementById('inboxList'));
  for (let index = 0; index < 5; index += 1) {
    root.append(element('div', { className: 'session-skeleton', 'aria-hidden': 'true' }, [
      element('span', { className: 'skeleton wide' }),
      element('span', { className: 'skeleton' }),
      element('span', { className: 'skeleton short' }),
    ]));
  }
}

function renderSessions() {
  const root = clear(document.getElementById('inboxList'));
  if (!State.sessions.length) {
    root.append(element('div', { className: 'empty-state' }, [
      element('strong', { text: 'No sessions match these filters.' }),
      element('span', { text: 'Vimgym keeps retained sessions available even when a provider root is offline.' }),
    ]));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const session of State.sessions) fragment.append(sessionCard(session));
  root.append(fragment);
}

function sessionCard(session) {
  const button = element('button', {
    className: 'session-card',
    type: 'button',
    'data-session-id': session.id,
    'aria-current': session.id === State.activeSessionId ? 'true' : 'false',
    'aria-label': `${session.title}, ${providerName(session.provider)}, ${session.kind}, ${session.lifecycle}`,
  });
  button.append(
    element('span', { className: 'session-card-header' }, [
      element('span', { className: 'session-workspace', text: session.workspaceLabel || 'No workspace' }),
      element('time', { text: relativeTime(session.updatedAt || session.startedAt), datetime: session.updatedAt || session.startedAt }),
    ]),
    element('span', { className: 'session-title', text: session.title }),
    element('span', { className: 'session-badges' }, [
      badge(providerName(session.provider), providerClass(session.provider)),
      badge(session.kind, `kind-${session.kind}`),
      badge(session.lifecycle, `lifecycle-${session.lifecycle}`),
      session.health === 'healthy' || session.health === 'ok' ? null : badge(session.health, `health-${session.health}`),
    ]),
  );
  const metadata = [];
  if (session.branch) metadata.push(`⎇ ${session.branch}`);
  if (session.messageCount) metadata.push(`${session.messageCount} messages`);
  if (session.originator && session.originator !== session.provider) metadata.push(`via ${session.originator}`);
  if (metadata.length) button.append(element('span', { className: 'session-meta', text: metadata.join(' · ') }));
  button.addEventListener('click', () => loadDetail(session.id));
  return button;
}

async function loadDetail(id, { focus = false } = {}) {
  if (!id) return;
  const requestId = ++State.detailRequest;
  State.activeSessionId = id;
  State.mode = 'READ';
  State.messages = [];
  State.messageCursor = null;
  State.messagePages = 0;
  State.transcriptQuery = '';
  renderSessions();
  renderDetailLoading();
  updateStatusbar();

  try {
    const rawSession = await apiFetch(`/api/sessions/${encodeURIComponent(id)}`);
    if (requestId !== State.detailRequest) return;
    State.activeSession = normalizeSession(rawSession);
    await loadMessages({ reset: true, requestId });
    if (requestId !== State.detailRequest) return;
    renderDetail();
    if (focus) document.getElementById('sessionDetail').focus();
  } catch (error) {
    if (requestId !== State.detailRequest) return;
    renderDetailError(`Could not load session: ${error.message}`);
  }
  updateStatusbar();
}

function renderDetailLoading() {
  const root = clear(document.getElementById('sessionDetail'));
  root.append(element('div', { className: 'detail-loading', role: 'status', text: 'Loading session transcript…' }));
}

function renderDetailError(message) {
  const root = clear(document.getElementById('sessionDetail'));
  root.append(element('div', { className: 'detail-error', role: 'alert', text: message }));
}

async function loadMessages({ reset = false, requestId = State.detailRequest } = {}) {
  if (State.loadingMessages || !State.activeSessionId) return;
  if (!reset && (!State.messageCursor || State.messagePages >= MAX_MESSAGE_PAGES)) return;
  State.loadingMessages = true;
  try {
    const body = await apiFetch(`/api/sessions/${encodeURIComponent(State.activeSessionId)}/messages` + queryString({
      limit: MESSAGE_PAGE_SIZE,
      cursor: reset ? null : State.messageCursor,
    }));
    if (requestId !== State.detailRequest) return;
    const envelope = normalizeEnvelope(body, 'messages');
    const normalized = envelope.items.map(normalizeMessage);
    State.messages = reset ? normalized : State.messages.concat(normalized);
    State.messages = State.messages.slice(0, MAX_MOUNTED_MESSAGES);
    State.messageCursor = envelope.nextCursor;
    State.messagePages = reset ? 1 : State.messagePages + 1;
  } catch (error) {
    if (reset && Array.isArray(State.activeSession?.messages)) {
      State.messages = State.activeSession.messages.slice(0, MAX_MOUNTED_MESSAGES).map(normalizeMessage);
      State.messageCursor = null;
      State.messagePages = 1;
      return;
    }
    throw error;
  } finally {
    State.loadingMessages = false;
  }
}

function renderDetail() {
  const session = State.activeSession;
  if (!session) return;
  const root = clear(document.getElementById('sessionDetail'));
  const header = element('header', { className: 'detail-header' });
  header.append(renderLineage(session));
  header.append(element('h2', { id: 'detailHeading', className: 'detail-title', text: session.title }));
  header.append(element('div', { className: 'detail-badges' }, [
    badge(providerName(session.provider), providerClass(session.provider)),
    badge(session.kind, `kind-${session.kind}`),
    badge(session.lifecycle, `lifecycle-${session.lifecycle}`),
    badge(session.health, `health-${session.health}`),
  ]));

  const facts = element('dl', { className: 'session-facts' });
  const factRows = [
    ['workspace', session.workspaceLabel || '—'],
    ['branch', session.branch || '—'],
    ['originator', session.originator || '—'],
    ['model', session.model || '—'],
    ['revision', session.revision ?? '—'],
    ['parser', session.parser_version || session.parserVersion || '—'],
  ];
  for (const [name, value] of factRows) facts.append(element('dt', { text: name }), element('dd', { text: value }));
  header.append(facts);

  const diagnostics = session.diagnostics || session.diagnostic;
  if (diagnostics) {
    const text = Array.isArray(diagnostics)
      ? diagnostics.map(item => typeof item === 'string' ? item : (item.message || item.code || 'diagnostic')).join(' · ')
      : String(diagnostics);
    header.append(element('div', { className: 'diagnostic-banner', role: 'status', text }));
  }
  root.append(header);

  const controls = element('div', { className: 'transcript-controls' });
  const searchLabel = element('label', { for: 'transcriptFilter', text: 'Filter loaded messages' });
  const searchInput = element('input', {
    id: 'transcriptFilter',
    type: 'search',
    value: State.transcriptQuery,
    placeholder: 'Role, text, tool, or result',
  });
  searchInput.addEventListener('input', () => {
    State.transcriptQuery = searchInput.value;
    renderTranscript();
  });
  controls.append(searchLabel, searchInput);
  controls.append(exportButton(session.id, 'markdown', 'Export Markdown'));
  controls.append(exportButton(session.id, 'canonical-jsonl', 'Export canonical JSONL'));
  root.append(controls);

  const summary = element('p', { id: 'transcriptSummary', className: 'transcript-summary' });
  root.append(summary);
  root.append(element('ol', { id: 'transcriptMessages', className: 'messages', 'aria-label': 'Session transcript' }));

  const pagination = element('div', { className: 'transcript-pagination' });
  const loadMore = element('button', {
    id: 'loadMoreMessages',
    className: 'secondary-button',
    type: 'button',
    text: State.messagePages >= MAX_MESSAGE_PAGES ? 'Transcript page limit reached' : 'Load more messages',
    hidden: !State.messageCursor,
    disabled: State.messagePages >= MAX_MESSAGE_PAGES,
  });
  loadMore.addEventListener('click', async () => {
    loadMore.disabled = true;
    loadMore.textContent = 'Loading…';
    try {
      await loadMessages();
      renderDetail();
      announce(`${State.messages.length} messages loaded`);
    } catch (error) {
      loadMore.disabled = false;
      loadMore.textContent = 'Retry loading messages';
      showToast(`Could not load more messages: ${error.message}`);
    }
  });
  pagination.append(loadMore);
  if (State.messagePages >= MAX_MESSAGE_PAGES && State.messageCursor) {
    pagination.append(element('p', {
      className: 'bounded-note',
      text: `The browser keeps at most ${MAX_MOUNTED_MESSAGES} messages mounted. Narrow the transcript filter or export the full session.`,
    }));
  }
  root.append(pagination);
  renderTranscript();
}

function lineageEntries(session) {
  if (Array.isArray(session.lineage) && session.lineage.length) {
    return session.lineage.map((entry, index) => ({
      id: sessionId(entry),
      label: entry.title || entry.kind || `session ${index + 1}`,
    }));
  }
  const entries = [];
  if (session.root_session && typeof session.root_session === 'object') {
    entries.push({ id: sessionId(session.root_session), label: session.root_session.title || 'root session' });
  } else if (session.rootId && session.rootId !== session.id) {
    entries.push({ id: session.rootId, label: 'root session' });
  }
  if (session.parent_session && typeof session.parent_session === 'object') {
    const id = sessionId(session.parent_session);
    if (!entries.some(entry => entry.id === id)) entries.push({ id, label: session.parent_session.title || 'parent session' });
  } else if (session.parentId && !entries.some(entry => entry.id === session.parentId)) {
    entries.push({ id: session.parentId, label: 'parent session' });
  }
  entries.push({ id: session.id, label: session.kind === 'subagent' ? 'subagent' : 'current session' });
  return entries;
}

function renderLineage(session) {
  const nav = element('nav', { className: 'lineage', 'aria-label': 'Session lineage' });
  const list = element('ol');
  for (const entry of lineageEntries(session)) {
    const item = element('li');
    if (entry.id && entry.id !== session.id) {
      const button = element('button', { type: 'button', className: 'lineage-link', text: entry.label });
      button.addEventListener('click', () => loadDetail(entry.id, { focus: true }));
      item.append(button);
    } else {
      item.append(element('span', { 'aria-current': 'page', text: entry.label }));
    }
    list.append(item);
  }
  nav.append(list);
  return nav;
}

function messageSearchText(message) {
  return [
    message.role,
    message.model,
    ...message.blocks.map(block => safeStringify({
      type: block.type,
      name: block.name,
      text: block.text,
      preview: block.preview,
      data: block.data || block.input,
      content: block.content,
    })),
  ].join('\n').toLocaleLowerCase();
}

function renderTranscript() {
  const root = document.getElementById('transcriptMessages');
  const summary = document.getElementById('transcriptSummary');
  if (!root || !summary) return;
  clear(root);
  const query = State.transcriptQuery.trim().toLocaleLowerCase();
  const visible = query
    ? State.messages.filter(message => messageSearchText(message).includes(query))
    : State.messages;
  summary.textContent = query
    ? `${visible.length} of ${State.messages.length} loaded messages match. Filtering does not fetch or search unloaded pages.`
    : `${State.messages.length} messages loaded${State.messageCursor ? '; more are available' : ''}.`;
  if (!visible.length) {
    root.append(element('li', { className: 'empty-state', text: query ? 'No loaded messages match.' : 'No visible messages were captured.' }));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const message of visible.slice(0, MAX_MOUNTED_MESSAGES)) fragment.append(renderMessage(message));
  root.append(fragment);
}

function renderMessage(message) {
  const item = element('li', { className: `message role-${message.role}` });
  const article = element('article', { 'aria-label': `${message.role} message ${message.sequence}` });
  const header = element('header', { className: 'message-header' });
  header.append(badge(message.role.toUpperCase(), `role-badge role-${message.role}`));
  if (message.model) header.append(element('span', { className: 'message-model', text: message.model }));
  if (message.turn !== undefined && message.turn !== null) header.append(element('span', { text: `turn ${message.turn}` }));
  if (message.timestamp) {
    header.append(element('time', {
      datetime: message.timestamp,
      text: new Date(message.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    }));
  }
  article.append(header);
  const body = element('div', { className: 'message-body' });
  if (!message.blocks.length) body.append(element('p', { className: 'omitted-block', text: '[no visible content]' }));
  for (const block of message.blocks) body.append(renderBlock(block));
  article.append(body);
  item.append(article);
  return item;
}

function blockText(block) {
  if (typeof block.text === 'string') return block.text;
  if (typeof block.preview === 'string') return block.preview;
  if (typeof block.content === 'string') return block.content;
  if (block.data !== undefined) return safeStringify(block.data);
  if (block.content !== undefined) return safeStringify(block.content);
  return '';
}

function renderBlock(block) {
  if (!block || typeof block !== 'object') return element('div', { className: 'unknown-block', text: '[invalid content block]' });
  const rawType = block.type || block.kind || 'unknown_event';
  const type = rawType === 'tool_use' ? 'tool_call'
    : rawType === 'image' ? 'attachment'
      : rawType === 'thinking' ? 'omitted'
        : rawType;
  const visibility = block.visibility || 'visible';

  if (visibility === 'hidden') {
    return element('div', { className: 'omitted-block', text: `[hidden ${type}]` });
  }
  if (type === 'text') {
    return previewContainer(block, element('p', { className: 'block-text', text: blockText(block).slice(0, MAX_PREVIEW_CHARS) }));
  }
  if (type === 'tool_call') return renderStructuredBlock(block, 'Tool call', block.name || block.tool_name || 'tool');
  if (type === 'tool_result') {
    const label = block.is_error || block.error ? 'Tool result · error' : 'Tool result';
    return renderStructuredBlock(block, label, block.name || block.tool_name || block.call_id || 'result');
  }
  if (type === 'attachment') {
    const wrapper = element('div', { className: 'attachment-block' });
    wrapper.append(badge('attachment', 'attachment-badge'));
    wrapper.append(element('span', {
      text: [block.name || block.filename || 'attachment', block.mime_type || block.mime].filter(Boolean).join(' · '),
    }));
    wrapper.append(element('span', { className: 'muted', text: 'Binary content is not stored.' }));
    return wrapper;
  }
  if (type === 'reasoning_summary' || type === 'compaction') {
    return renderStructuredBlock(block, type === 'compaction' ? 'Compaction summary' : 'Visible reasoning summary', type);
  }
  if (type === 'omitted') return element('div', { className: 'omitted-block', text: blockText(block) || '[provider content omitted]' });
  return element('div', { className: 'unknown-block', text: `[unsupported provider event: ${type}]` });
}

function renderStructuredBlock(block, label, name) {
  const details = element('details', { className: `structured-block${block.is_error || block.error ? ' block-error' : ''}` });
  const summary = element('summary');
  summary.append(badge(label, 'block-type'), element('span', { className: 'block-name', text: name }));
  details.append(summary);
  const content = blockText(block).slice(0, MAX_PREVIEW_CHARS);
  const pre = element('pre', { className: 'structured-content', tabindex: '0', text: content || '[empty]' });
  details.append(previewContainer(block, pre));
  const copy = element('button', { className: 'copy-button', type: 'button', text: 'Copy redacted content' });
  copy.addEventListener('click', () => copyText(content, copy));
  details.append(copy);
  return details;
}

function previewContainer(block, contentNode) {
  const wrapper = element('div', { className: 'block-preview' });
  wrapper.append(contentNode);
  const contentUrl = block.content_url || block.full_content_url;
  if (contentUrl) {
    const load = element('button', { className: 'secondary-button load-full-block', type: 'button', text: 'Load full redacted block' });
    load.addEventListener('click', () => loadFullBlock(contentUrl, wrapper, load));
    wrapper.append(load);
    wrapper.append(element('span', { className: 'muted', text: 'This preview was truncated.' }));
  }
  return wrapper;
}

async function loadFullBlock(url, wrapper, button) {
  if (!String(url).startsWith('/api/message-blocks/')) {
    showToast('Blocked an unexpected content URL');
    return;
  }
  button.disabled = true;
  button.textContent = 'Loading…';
  try {
    const full = await apiFetch(url);
    const text = blockText(full);
    const output = element('pre', { className: 'structured-content full-block', tabindex: '0', text });
    wrapper.replaceChildren(output);
    const copy = element('button', { className: 'copy-button', type: 'button', text: 'Copy redacted content' });
    copy.addEventListener('click', () => copyText(text, copy));
    wrapper.append(copy);
    announce('Full redacted block loaded');
  } catch (error) {
    button.disabled = false;
    button.textContent = 'Retry full block';
    showToast(`Could not load block: ${error.message}`);
  }
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    const previous = button.textContent;
    button.textContent = 'Copied';
    window.setTimeout(() => { button.textContent = previous; }, 1400);
  } catch (_error) {
    showToast('Clipboard access was unavailable');
  }
}

function exportButton(id, format, label) {
  const button = element('button', { className: 'secondary-button', type: 'button', text: label });
  button.addEventListener('click', () => exportSession(id, format, button));
  return button;
}

async function exportSession(id, format, button) {
  button.disabled = true;
  try {
    const response = await fetch(`/api/sessions/${encodeURIComponent(id)}/export` + queryString({ format }));
    if (!response.ok) throw new ApiError(response.status, `Export failed (${response.status})`);
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const download = element('a', { href: objectUrl });
    const suffix = format === 'canonical-jsonl' ? 'jsonl' : 'md';
    download.download = `vimgym-${id.slice(0, 8)}.${suffix}`;
    document.body.append(download);
    download.click();
    download.remove();
    URL.revokeObjectURL(objectUrl);
    announce(`${labelForFormat(format)} export ready`);
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
}

function labelForFormat(format) {
  return format === 'canonical-jsonl' ? 'Canonical JSONL' : 'Markdown';
}

function openSearch() {
  if (State.searchOpen) return;
  State.searchOpen = true;
  State.mode = 'SEARCH';
  State.returnFocus = document.activeElement;
  document.querySelector('.app').inert = true;
  document.getElementById('searchLauncher').setAttribute('aria-expanded', 'true');
  const overlay = document.getElementById('commandOverlay');
  overlay.hidden = false;
  const input = document.getElementById('commandInput');
  input.value = '';
  input.removeAttribute('aria-activedescendant');
  clear(document.getElementById('commandResults'));
  State.searchResults = [];
  State.searchCursor = null;
  State.searchSelected = 0;
  window.setTimeout(() => input.focus(), 0);
  updateStatusbar();
}

// Keep the transition-era public name while the behavior targets
// provider-neutral block search.
function openCommandPalette() {
  openSearch();
}

function closeSearch() {
  if (!State.searchOpen) return;
  State.searchOpen = false;
  State.mode = State.activeSession ? 'READ' : 'NORMAL';
  document.getElementById('commandOverlay').hidden = true;
  document.querySelector('.app').inert = false;
  document.getElementById('searchLauncher').setAttribute('aria-expanded', 'false');
  if (State.searchAbort) State.searchAbort.abort();
  if (State.returnFocus && typeof State.returnFocus.focus === 'function') State.returnFocus.focus();
  updateStatusbar();
}

async function runSearch(query, { append = false } = {}) {
  const root = document.getElementById('commandResults');
  if (!query.trim()) {
    State.searchResults = [];
    State.searchCursor = null;
    clear(root).append(element('p', { className: 'command-empty', text: 'Enter text to search all redacted providers.' }));
    return;
  }
  if (State.searchAbort) State.searchAbort.abort();
  State.searchAbort = new AbortController();
  setBusy(root, true);
  if (!append) clear(root).append(element('p', { className: 'command-empty', text: 'Searching…' }));
  try {
    const body = await apiFetch('/api/search' + queryString({
      q: query,
      ...State.filters,
      limit: 20,
      cursor: append ? State.searchCursor : null,
    }), { signal: State.searchAbort.signal });
    const envelope = normalizeEnvelope(body, 'results');
    const results = envelope.items.map(result => ({ ...result, session: normalizeSession(result.session || result) }));
    State.searchResults = append ? State.searchResults.concat(results) : results;
    State.searchCursor = envelope.nextCursor;
    State.searchSelected = append ? State.searchSelected : 0;
    renderSearchResults();
    announce(`${State.searchResults.length} search results loaded`);
  } catch (error) {
    if (error.name !== 'AbortError') {
      clear(root).append(element('p', { className: 'command-error', role: 'alert', text: `Search failed: ${error.message}` }));
    }
  } finally {
    setBusy(root, false);
  }
}

function renderSearchResults() {
  const root = clear(document.getElementById('commandResults'));
  const input = document.getElementById('commandInput');
  if (!State.searchResults.length) {
    root.append(element('p', { className: 'command-empty', text: 'No matches.' }));
    input.removeAttribute('aria-activedescendant');
    return;
  }
  State.searchResults.forEach((result, index) => {
    const session = result.session;
    const option = element('button', {
      id: `search-result-${index}`,
      className: `command-result${index === State.searchSelected ? ' selected' : ''}`,
      type: 'button',
      role: 'option',
      'aria-selected': index === State.searchSelected ? 'true' : 'false',
    });
    option.append(element('span', { className: 'command-result-title', text: session.title }));
    option.append(element('span', { className: 'command-result-meta' }, [
      badge(providerName(session.provider), providerClass(session.provider)),
      badge(session.kind, `kind-${session.kind}`),
      element('span', { text: session.workspaceLabel }),
    ]));
    const snippet = element('span', { className: 'command-result-snippet' });
    const parts = Array.isArray(result.snippet_parts)
      ? result.snippet_parts
      : [{ text: typeof result.snippet === 'string' ? result.snippet : '', matched: false }];
    for (const part of parts) {
      const node = part.matched ? element('mark', { text: part.text }) : document.createTextNode(String(part.text || ''));
      snippet.append(node);
    }
    option.append(snippet);
    option.addEventListener('click', () => openSearchResult(index));
    root.append(option);
  });
  if (State.searchCursor) {
    const more = element('button', { className: 'secondary-button command-more', type: 'button', text: 'Load more search results' });
    more.addEventListener('click', () => runSearch(input.value, { append: true }));
    root.append(more);
  }
  updateSearchSelection();
}

function updateSearchSelection() {
  const input = document.getElementById('commandInput');
  const options = [...document.querySelectorAll('.command-result[role="option"]')];
  options.forEach((option, index) => {
    const selected = index === State.searchSelected;
    option.classList.toggle('selected', selected);
    option.setAttribute('aria-selected', String(selected));
  });
  const active = options[State.searchSelected];
  if (active) {
    input.setAttribute('aria-activedescendant', active.id);
    active.scrollIntoView({ block: 'nearest' });
  }
}

function moveSearchSelection(delta) {
  if (!State.searchResults.length) return;
  State.searchSelected = (State.searchSelected + delta + State.searchResults.length) % State.searchResults.length;
  updateSearchSelection();
}

function trapSearchFocus(event) {
  if (!State.searchOpen || event.key !== 'Tab') return;
  const palette = document.querySelector('.command-palette');
  const focusable = [...palette.querySelectorAll('button:not(:disabled), input:not(:disabled)')]
    .filter(node => !node.hidden);
  if (!focusable.length) return;
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

function openSearchResult(index) {
  const result = State.searchResults[index];
  if (!result) return;
  const id = result.session.id || result.session_id || result.session_uuid;
  closeSearch();
  loadDetail(id, { focus: true });
}

async function openSettings() {
  State.activeSession = null;
  State.activeSessionId = null;
  State.mode = 'NORMAL';
  renderSessions();
  updateStatusbar();
  const root = clear(document.getElementById('sessionDetail'));
  root.append(element('div', { className: 'settings-loading', role: 'status', text: 'Loading vault status…' }));
  try {
    const [config, sourceBody] = await Promise.all([
      firstAvailable(['/api/config']),
      firstAvailable(['/api/sources', '/api/config/sources']),
    ]);
    clear(root);
    root.append(element('header', { className: 'detail-header' }, [
      element('h2', { id: 'detailHeading', className: 'detail-title', text: 'Vault settings' }),
      element('p', { className: 'privacy-note', text: 'Stored content is credential-scrubbed, not anonymized. Portable backups remain sensitive.' }),
    ]));
    const section = element('section', { className: 'settings-section' });
    section.append(element('h3', { text: 'Local service' }));
    const facts = element('dl', { className: 'settings-facts' });
    const rows = [
      ['bind', config?.server_host || config?.host || 'loopback only'],
      ['port', config?.server_port || config?.port || '—'],
      ['debounce', config?.debounce_secs === undefined ? '5 seconds' : `${config.debounce_secs} seconds`],
      ['vault', config?.vault_dir || '—'],
    ];
    for (const [name, value] of rows) facts.append(element('dt', { text: name }), element('dd', { text: value }));
    section.append(facts);
    root.append(section);
    State.sources = Array.isArray(sourceBody)
      ? sourceBody
      : sourceBody && Array.isArray(sourceBody.sources)
      ? sourceBody.sources
      : (Array.isArray(sourceBody?.items) ? sourceBody.items : State.sources);
    const sourceSection = element('section', { className: 'settings-section' });
    sourceSection.append(element('h3', { text: 'Configured sources' }));
    const list = element('ul', { className: 'settings-source-list' });
    for (const source of State.sources) {
      list.append(element('li', {}, [
        badge(providerName(source.provider || source.type || source.id), providerClass(source.provider || source.type || source.id)),
        element('span', { text: source.path || source.root || 'configured root' }),
        badge(source.health || source.status || (source.enabled === false ? 'disabled' : 'healthy')),
      ]));
    }
    if (!State.sources.length) list.append(element('li', { className: 'muted', text: 'No sources reported.' }));
    sourceSection.append(list);
    root.append(sourceSection);
  } catch (error) {
    renderDetailError(`Could not load vault settings: ${error.message}`);
  }
}

function socketEventSessionId(message) {
  const payload = message.session || message.payload || message;
  return payload.id || payload.session_id || payload.session_uuid || '';
}

function updateConnection(state, label) {
  const button = document.getElementById('connectionStatus');
  button.dataset.state = state;
  document.getElementById('connectionLabel').textContent = label;
  document.getElementById('sbWatch').textContent = `live updates ${label}`;
}

function connectWebSocket({ immediate = false } = {}) {
  window.clearTimeout(State.reconnectTimer);
  if (State.socket) {
    State.socket.onclose = null;
    State.socket.close();
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host || '127.0.0.1:7337';
  updateConnection('connecting', immediate ? 'reconnecting' : 'connecting');
  let socket;
  try {
    socket = new WebSocket(`${protocol}//${host}/ws`);
  } catch (_error) {
    scheduleReconnect();
    return;
  }
  State.socket = socket;
  socket.onopen = () => {
    const reconnected = State.reconnectAttempt > 0;
    State.reconnectAttempt = 0;
    updateConnection('connected', 'connected');
    if (reconnected) {
      loadSessions();
      loadAuxiliaryData();
      if (State.activeSessionId) loadDetail(State.activeSessionId);
      announce('Live updates reconnected and refreshed');
    }
  };
  socket.onmessage = event => {
    let message;
    try { message = JSON.parse(event.data); } catch (_error) { return; }
    const type = message.type;
    if (['session_created', 'session_updated', 'session_archived'].includes(type)) {
      const id = socketEventSessionId(message);
      loadSessions();
      loadAuxiliaryData();
      const verbs = { session_created: 'created', session_updated: 'updated', session_archived: 'archived' };
      announce(`Session ${verbs[type]}`);
      if (id && id === State.activeSessionId) loadDetail(id);
    } else if (type === 'source_health_changed') {
      loadAuxiliaryData();
      announce('Source health changed');
    }
  };
  socket.onerror = () => socket.close();
  socket.onclose = () => scheduleReconnect();
}

function scheduleReconnect() {
  State.reconnectAttempt += 1;
  const delay = Math.min(30000, 1000 * (2 ** Math.min(State.reconnectAttempt - 1, 5)));
  updateConnection('disconnected', `retrying in ${Math.round(delay / 1000)}s`);
  window.clearTimeout(State.reconnectTimer);
  State.reconnectTimer = window.setTimeout(() => connectWebSocket(), delay);
}

function updateStatusbar() {
  document.getElementById('sbMode').textContent = State.mode;
  document.getElementById('sbCount').textContent = `${State.sessionTotal} session${State.sessionTotal === 1 ? '' : 's'}`;
  const context = State.activeSession
    ? `${State.activeSession.workspaceLabel || 'no workspace'} / ${State.activeSession.branch || 'no branch'}`
    : Object.entries(State.filters).filter(([, value]) => value).map(([key, value]) => `${key}:${value}`).join(' ') || 'all sessions';
  document.getElementById('sbContext').textContent = context;
  document.getElementById('sbSession').textContent = State.activeSessionId ? State.activeSessionId.slice(0, 8) : '';
}

function closeDetail() {
  State.activeSession = null;
  State.activeSessionId = null;
  State.mode = 'NORMAL';
  State.detailRequest += 1;
  renderSessions();
  const root = clear(document.getElementById('sessionDetail'));
  root.append(element('div', { className: 'welcome' }, [
    element('div', { className: 'welcome-logo', 'aria-hidden': 'true' }, [
      element('span', { className: 'welcome-vim', text: 'vim' }),
      element('span', { className: 'welcome-gym', text: 'gym' }),
    ]),
    element('div', { className: 'glow-line', 'aria-hidden': 'true' }),
    element('h2', { id: 'detailHeading', text: 'Provider-neutral AI session memory' }),
    element('p', { text: 'Choose a session, or press Command K to search across redacted captures.' }),
  ]));
  updateStatusbar();
}

function setupEvents() {
  document.getElementById('searchLauncher').addEventListener('click', openCommandPalette);
  document.getElementById('closeSearch').addEventListener('click', closeSearch);
  document.getElementById('settingsBtn').addEventListener('click', openSettings);
  document.getElementById('connectionStatus').addEventListener('click', () => connectWebSocket({ immediate: true }));
  document.getElementById('loadMoreSessions').addEventListener('click', () => loadSessions({ append: true }));

  const filterForm = document.getElementById('filterForm');
  filterForm.addEventListener('change', () => {
    readFilters();
    loadSessions();
    updateStatusbar();
  });
  filterForm.addEventListener('submit', event => event.preventDefault());
  document.getElementById('clearFilters').addEventListener('click', () => {
    filterForm.reset();
    readFilters();
    loadSessions();
    updateStatusbar();
  });

  const input = document.getElementById('commandInput');
  input.addEventListener('input', () => {
    window.clearTimeout(State.searchTimer);
    State.searchTimer = window.setTimeout(() => runSearch(input.value), 180);
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      moveSearchSelection(1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      moveSearchSelection(-1);
    } else if (event.key === 'Enter' && State.searchResults.length) {
      event.preventDefault();
      openSearchResult(State.searchSelected);
    }
  });

  document.getElementById('commandOverlay').addEventListener('click', event => {
    if (event.target.id === 'commandOverlay') closeSearch();
  });

  document.addEventListener('keydown', event => {
    trapSearchFocus(event);
    if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === 'k') {
      event.preventDefault();
      openSearch();
      return;
    }
    if (event.key === 'Escape') {
      if (State.searchOpen) closeSearch();
      else if (State.activeSessionId) closeDetail();
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  setupEvents();
  renderMatrix();
  readFilters();
  loadSessions();
  loadAuxiliaryData();
  connectWebSocket();
});
