// vimgym — neon void web UI
// vanilla JS, no framework, no build step

// ── STATE ────────────────────────────────────────────────────────────
const State = {
  sessions: [],
  activeSession: null,
  activeProject: null,
  activeBranch: null,
  searchQuery: '',
  cmdPaletteOpen: false,
  cmdSelectedIndex: 0,
  cmdResults: [],
  mode: 'NORMAL',
  stats: null,
  health: null,
  inboxOffset: 0,
  inboxTotal: 0,
  inboxLimit: 50,
  loading: false,
  cmdSearchTimer: null,
};

// ── API HELPERS ──────────────────────────────────────────────────────
async function apiFetch(url) {
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    console.error(`API error ${url}:`, err);
    return null;
  }
}

function qs(params) {
  const filtered = Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== '')
  );
  const s = new URLSearchParams(filtered).toString();
  return s ? `?${s}` : '';
}

// ── UTILITIES ────────────────────────────────────────────────────────
function relativeTime(isoString) {
  if (!isoString) return '';
  const diff = Date.now() - new Date(isoString).getTime();
  const mins = Math.floor(diff / 60000);
  const hours = Math.floor(mins / 60);
  const days = Math.floor(hours / 24);
  if (mins < 1)    return 'just now';
  if (mins < 60)   return `${mins}m ago`;
  if (hours < 24)  return `${hours}h ago`;
  if (days === 1)  return 'yesterday';
  if (days < 7)    return `${days}d ago`;
  return new Date(isoString).toLocaleDateString('en', { month: 'short', day: 'numeric' });
}

function formatDuration(secs) {
  if (!secs) return '';
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function formatBytes(b) {
  if (!b) return '0 B';
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / 1024 / 1024).toFixed(1)} MB`;
}

function formatTokens(n) {
  if (!n) return '0';
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return String(n);
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function toolChipClass(name) {
  // matches CSS .tc-{lowercased} classes
  return `tool-chip tc-${(name || '').toLowerCase()}`;
}

// ── MATRIX RAIN ──────────────────────────────────────────────────────
(function matrixRain() {
  const canvas = document.getElementById('matrix-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let cols, drops;
  const chars = 'アイウエオカキクケコサシスセソタチツテトナニヌネノ0123456789ABCDEF><{}[]|\\'.split('');

  function init() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    cols = Math.floor(canvas.width / 16);
    drops = Array(cols).fill(0).map(() => Math.random() * -100);
  }

  function draw() {
    ctx.fillStyle = 'rgba(6,6,8,0.05)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#00FF41';
    ctx.font = '12px JetBrains Mono, monospace';
    for (let i = 0; i < drops.length; i++) {
      const ch = chars[Math.floor(Math.random() * chars.length)];
      ctx.globalAlpha = Math.random() * 0.8 + 0.1;
      ctx.fillText(ch, i * 16, drops[i] * 16);
      if (drops[i] * 16 > canvas.height && Math.random() > 0.975) drops[i] = 0;
      drops[i] += 0.4;
    }
    ctx.globalAlpha = 1;
  }

  init();
  window.addEventListener('resize', init);
  setInterval(draw, 50);
})();

// ── SIDEBAR ──────────────────────────────────────────────────────────
async function loadSidebar() {
  const [projects, stats, timeline, sessionsForBranches] = await Promise.all([
    apiFetch('/api/projects'),
    apiFetch('/api/stats'),
    apiFetch('/api/stats/timeline?since=365d'),
    apiFetch('/api/sessions?limit=200'),
  ]);

  State.stats = stats;
  renderProjects(projects, stats);
  renderBranches(sessionsForBranches);
  renderHeatmap(timeline);
  renderStats(stats);
  renderToolsList(stats);
  updateStatusbar();
}

function renderProjects(projects, stats) {
  const root = document.getElementById('sidebarProjects');
  if (!root) return;
  if (!projects) { root.innerHTML = '<div class="error-state">— failed to load —</div>'; return; }

  const total = stats ? stats.total_sessions : projects.reduce((a, p) => a + (p.session_count || 0), 0);
  const items = [
    `<div class="sidebar-item ${State.activeProject === null ? 'active' : ''}" data-project="">
       <div class="sidebar-item-name"><span class="sidebar-icon">◆</span> All sessions</div>
       <span class="badge badge-cyan">${total}</span>
     </div>`,
    ...projects.map(p => `
      <div class="sidebar-item ${State.activeProject === p.project_name ? 'active' : ''}" data-project="${escapeHtml(p.project_name)}">
        <div class="sidebar-item-name"><span class="sidebar-icon">◈</span> ${escapeHtml(p.project_name)}</div>
        <span class="badge badge-green">${p.session_count}</span>
      </div>
    `),
  ];
  root.innerHTML = items.join('');

  root.querySelectorAll('.sidebar-item').forEach(el => {
    el.addEventListener('click', () => {
      const proj = el.dataset.project || null;
      State.activeProject = proj;
      State.activeBranch = null;
      loadInbox();
      // refresh active class without re-fetching
      root.querySelectorAll('.sidebar-item').forEach(e => e.classList.remove('active'));
      el.classList.add('active');
      document.querySelectorAll('#sidebarBranches .sidebar-item').forEach(e => e.classList.remove('active'));
    });
  });
}

function renderBranches(sessionData) {
  const root = document.getElementById('sidebarBranches');
  if (!root) return;
  if (!sessionData) { root.innerHTML = '<div class="error-state">—</div>'; return; }

  const counts = new Map();
  for (const s of sessionData.sessions) {
    if (!s.git_branch) continue;
    counts.set(s.git_branch, (counts.get(s.git_branch) || 0) + 1);
  }
  const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12);

  root.innerHTML = sorted.map(([branch, n]) => `
    <div class="sidebar-item" data-branch="${escapeHtml(branch)}">
      <div class="sidebar-item-name"><span class="sidebar-icon">⎇</span> ${escapeHtml(branch)}</div>
      <span class="badge ${n >= 4 ? 'badge-green' : 'badge-cyan'}">${n}</span>
    </div>
  `).join('');

  root.querySelectorAll('.sidebar-item').forEach(el => {
    el.addEventListener('click', () => {
      State.activeBranch = el.dataset.branch;
      loadInbox();
      root.querySelectorAll('.sidebar-item').forEach(e => e.classList.remove('active'));
      el.classList.add('active');
    });
  });
}

function renderHeatmap(timeline) {
  const root = document.getElementById('heatmap');
  if (!root) return;
  // Build a map from date → count
  const byDate = new Map();
  if (timeline && timeline.days) {
    for (const d of timeline.days) byDate.set(d.date, d.count);
  }

  // 26 weeks × 7 days = 182 cells
  const today = new Date();
  const cells = [];
  for (let i = 181; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(d.getDate() - i);
    const iso = d.toISOString().slice(0, 10);
    const count = byDate.get(iso) || 0;
    let lvl = 0;
    if (count >= 4) lvl = 4;
    else if (count >= 3) lvl = 3;
    else if (count >= 2) lvl = 2;
    else if (count >= 1) lvl = 1;
    cells.push(`<div class="hm-cell ${lvl ? 'hm-' + lvl : ''}" title="${iso}: ${count}"></div>`);
  }
  root.innerHTML = cells.join('');
}

function renderStats(stats) {
  const root = document.getElementById('sidebarStats');
  if (!root) return;
  if (!stats) {
    root.innerHTML = `
      <div class="stat-row"><span>total sessions</span><span class="stat-val">—</span></div>
      <div class="stat-row"><span>ai time</span><span class="stat-val">—</span></div>
    `;
    return;
  }
  root.innerHTML = `
    <div class="stat-row"><span>total sessions</span><span class="stat-val">${stats.total_sessions}</span></div>
    <div class="stat-row"><span>ai time</span><span class="stat-val">${formatDuration(stats.total_duration_secs)}</span></div>
    <div class="stat-row"><span>db size</span><span class="stat-val">${formatBytes(stats.db_size_bytes)}</span></div>
    <div class="stat-row"><span>output tokens</span><span class="stat-val">${formatTokens(stats.total_output_tokens)}</span></div>
  `;
}

function renderToolsList(stats) {
  const root = document.getElementById('sidebarTools');
  if (!root) return;
  if (!stats || !stats.top_tools) { root.innerHTML = ''; return; }
  root.innerHTML = stats.top_tools
    .map(t => `<span class="${toolChipClass(t.tool)}" title="${t.count} sessions">${escapeHtml(t.tool)}</span>`)
    .join('');
}

// ── INBOX ────────────────────────────────────────────────────────────
function showInboxSkeleton() {
  const list = document.getElementById('inboxList');
  list.innerHTML = Array(5).fill(0).map(() => `
    <div class="session-skeleton">
      <div class="sk-line skeleton"></div>
      <div class="sk-line skeleton"></div>
      <div class="sk-line skeleton"></div>
    </div>
  `).join('');
}

async function loadInbox(append = false) {
  if (State.loading) return;
  State.loading = true;

  if (!append) {
    State.inboxOffset = 0;
    showInboxSkeleton();
  }

  const params = {
    limit: State.inboxLimit,
    offset: State.inboxOffset,
    project: State.activeProject,
    branch: State.activeBranch,
  };
  const data = await apiFetch('/api/sessions' + qs(params));

  State.loading = false;

  if (!data) {
    document.getElementById('inboxList').innerHTML =
      '<div class="error-state">⚠ Could not load sessions</div>';
    return;
  }

  State.inboxTotal = data.total;
  if (append) {
    State.sessions = State.sessions.concat(data.sessions);
  } else {
    State.sessions = data.sessions;
  }
  State.inboxOffset += data.sessions.length;

  renderInbox(append);
  document.getElementById('inboxCount').textContent = `${State.inboxTotal} backed up`;
  updateStatusbar();
}

function renderInbox(append = false) {
  const list = document.getElementById('inboxList');
  if (!State.sessions.length) {
    list.innerHTML = `
      <div class="empty-state">
        ⬡  vimgym is watching<br>
        Start a Claude Code session to see it here.<br>
        <span class="blink"></span>
      </div>
    `;
    return;
  }

  const html = State.sessions.map(s => sessionCardHTML(s)).join('');
  if (append) {
    list.insertAdjacentHTML('beforeend', html);
  } else {
    list.innerHTML = html;
  }
  attachCardClickHandlers();
}

function sessionCardHTML(s) {
  const tools = (s.tools_used || []).slice(0, 3);
  const toolChips = tools.map(t =>
    `<span class="${toolChipClass(t)}" style="font-size:8px">${escapeHtml(t)}</span>`
  ).join('');

  return `
    <div class="session-card" data-uuid="${escapeHtml(s.session_uuid)}">
      <div class="sc-header">
        <span class="sc-project">${escapeHtml(s.project_name || '')}</span>
        <span class="sc-time">${escapeHtml(relativeTime(s.started_at))}</span>
      </div>
      <div class="sc-title">${escapeHtml(s.ai_title || '(untitled session)')}</div>
      <div class="sc-meta">
        ${s.git_branch ? `<span class="sc-branch">⎇ ${escapeHtml(s.git_branch)}</span>` : ''}
        ${s.duration_secs ? `<span class="sc-duration">${formatDuration(s.duration_secs)}</span>` : ''}
        ${s.has_subagents ? '<span class="sc-subagent">⬡ subagents</span>' : ''}
        ${toolChips}
      </div>
    </div>
  `;
}

function attachCardClickHandlers() {
  document.querySelectorAll('.session-card').forEach(el => {
    el.addEventListener('click', () => {
      const uuid = el.dataset.uuid;
      if (!uuid) return;
      document.querySelectorAll('.session-card').forEach(c => c.classList.remove('active'));
      el.classList.add('active');
      loadDetail(uuid);
    });
  });
}

// Infinite scroll
function setupInboxScroll() {
  const list = document.getElementById('inboxList');
  list.addEventListener('scroll', () => {
    if (State.loading) return;
    if (State.sessions.length >= State.inboxTotal) return;
    if (list.scrollTop + list.clientHeight >= list.scrollHeight - 200) {
      loadInbox(true);
    }
  });
}

// ── DETAIL ───────────────────────────────────────────────────────────
async function loadDetail(uuidPrefix) {
  const detail = document.getElementById('detail');
  detail.innerHTML = `
    <div class="detail-header">
      <div class="detail-breadcrumb"><span class="sep">loading…</span></div>
    </div>
    <div class="messages"></div>
  `;

  const session = await apiFetch(`/api/sessions/${encodeURIComponent(uuidPrefix)}`);
  if (!session) {
    detail.innerHTML = '<div class="error-state">⚠ Could not load session</div>';
    return;
  }

  State.activeSession = session;
  State.mode = 'READ';
  renderDetail(session);
  updateStatusbar();
}

function renderDetail(s) {
  const detail = document.getElementById('detail');
  const tools = s.tools_used || [];
  const subagentChip = s.has_subagents
    ? `<span class="meta-chip pink">⬡ subagents</span>` : '';
  const dateStr = s.started_at ? new Date(s.started_at).toLocaleDateString('en', {
    year: 'numeric', month: 'short', day: 'numeric'
  }) : '';

  detail.innerHTML = `
    <div class="detail-header">
      <div class="detail-breadcrumb">
        <span class="project">${escapeHtml(s.project_name || '')}</span>
        ${s.git_branch ? '<span class="sep">/</span><span class="branch">' + escapeHtml(s.git_branch) + '</span>' : ''}
        <span class="sep">·</span>
        <span>${escapeHtml((s.session_uuid || '').slice(0, 8))}</span>
      </div>
      <div class="detail-title">${escapeHtml(s.ai_title || '(untitled session)')}</div>
      <div class="detail-meta-row">
        ${s.duration_secs ? '<span class="meta-chip green">' + formatDuration(s.duration_secs) + '</span>' : ''}
        <span class="meta-chip cyan">${s.message_count || 0} messages</span>
        ${subagentChip}
        ${s.claude_version ? '<span class="meta-chip amber">' + escapeHtml(s.claude_version) + '</span>' : ''}
        ${s.slug ? '<span class="meta-chip">' + escapeHtml(s.slug) + '</span>' : ''}
        ${dateStr ? '<span class="meta-chip">' + escapeHtml(dateStr) + '</span>' : ''}
      </div>
      <div class="detail-tools">
        ${tools.map(t => '<span class="' + toolChipClass(t) + '">' + escapeHtml(t) + '</span>').join('')}
      </div>
    </div>
    <div class="messages" id="messages"></div>
    <div class="detail-footer">
      <button class="topbar-btn" id="exportBtn">↓ Export Markdown</button>
      <span style="font-size:10px;color:var(--text-lo)">${s.cwd ? escapeHtml(s.cwd) : ''}</span>
    </div>
  `;

  const messagesEl = document.getElementById('messages');
  messagesEl.innerHTML = (s.messages || []).map(m => messageHTML(m)).join('');

  // Highlight code blocks
  if (window.hljs) {
    messagesEl.querySelectorAll('pre code').forEach(block => {
      try { window.hljs.highlightElement(block); } catch (_) {}
    });
  }

  // Wire copy buttons
  messagesEl.querySelectorAll('.copy-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const codeEl = btn.parentElement.querySelector('pre');
      if (!codeEl) return;
      navigator.clipboard.writeText(codeEl.textContent || '').then(() => {
        const orig = btn.textContent;
        btn.textContent = 'copied!';
        setTimeout(() => { btn.textContent = orig; }, 1500);
      });
    });
  });

  // Export
  document.getElementById('exportBtn').addEventListener('click', () => exportSession(s.session_uuid));
}

// ── CONTENT RENDERING ────────────────────────────────────────────────
function messageHTML(m) {
  const role = m.role || 'user';
  const ts = (m.timestamp || '').slice(11, 19); // HH:MM:SS
  const blocksHTML = (m.content || []).map(b => contentBlockHTML(b, role)).join('');

  return `
    <div class="msg ${role}">
      <div class="msg-header">
        <span class="msg-role role-${role}">${role === 'user' ? 'USER' : 'CLAUDE'}</span>
        <span class="msg-ts">${escapeHtml(ts)}</span>
      </div>
      <div class="msg-body">
        ${blocksHTML}
      </div>
    </div>
  `;
}

function contentBlockHTML(block, role) {
  if (!block || typeof block !== 'object') return '';
  const t = block.type;

  if (t === 'text') {
    return `<div class="msg-text">${renderMarkdownLite(block.text || '')}</div>`;
  }

  if (t === 'tool_use') {
    const name = block.name || 'tool';
    const input = block.input || {};
    const desc = input.description || input.command || input.file_path || input.query || input.path || 'tool input';
    const inputJson = JSON.stringify(input, null, 2);
    return `
      <details class="tool-block">
        <summary>
          <span class="tool-arrow">▶</span>
          <span class="tool-name-badge ${toolChipClass(name).replace('tool-chip ', '')}">${escapeHtml(name)}</span>
          <span class="tool-desc">${escapeHtml(String(desc).slice(0, 120))}</span>
          <span class="tool-status">tool_use</span>
        </summary>
        <div class="tool-content">
          <div class="code-wrap">
            <pre class="code-block"><code class="language-json">${escapeHtml(inputJson)}</code></pre>
            <button class="copy-btn">copy</button>
          </div>
        </div>
      </details>
    `;
  }

  if (t === 'tool_result') {
    const text = extractToolResultText(block.content);
    if (!text) return '';
    return `
      <details class="tool-block">
        <summary>
          <span class="tool-arrow">▶</span>
          <span class="tool-name-badge" style="border-color:var(--void-5);color:var(--text-mid)">tool_result</span>
          <span class="tool-desc">${escapeHtml(text.slice(0, 120))}</span>
        </summary>
        <div class="tool-content">
          <div class="code-wrap">
            <pre class="code-block"><code>${escapeHtml(text.slice(0, 8000))}</code></pre>
            <button class="copy-btn">copy</button>
          </div>
        </div>
      </details>
    `;
  }

  if (t === 'image') {
    return `<div class="img-omitted">[image omitted — not stored]</div>`;
  }

  if (t === 'thinking') {
    return `
      <details class="tool-block">
        <summary>
          <span class="tool-arrow">▶</span>
          <span class="tool-name-badge" style="border-color:var(--purple);color:var(--purple)">thinking</span>
          <span class="tool-desc">internal reasoning omitted to save space</span>
        </summary>
        <div class="tool-content" style="color:var(--text-lo);font-size:11px">
          Thinking blocks are not stored.
        </div>
      </details>
    `;
  }

  return '';
}

function extractToolResultText(content) {
  if (content == null) return '';
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.map(b => (b && typeof b === 'object' ? (b.text || '') : String(b))).join('\n');
  }
  return JSON.stringify(content);
}

// Markdown-lite: only `code`, **bold**, and ```fenced``` blocks
function renderMarkdownLite(text) {
  if (!text) return '';
  // Fenced code blocks first (so their contents aren't reprocessed)
  const fenceRe = /```(\w+)?\n([\s\S]*?)```/g;
  const parts = [];
  let lastIndex = 0;
  let m;
  while ((m = fenceRe.exec(text)) !== null) {
    parts.push({ type: 'text', value: text.slice(lastIndex, m.index) });
    parts.push({ type: 'code', lang: m[1] || '', value: m[2] });
    lastIndex = m.index + m[0].length;
  }
  parts.push({ type: 'text', value: text.slice(lastIndex) });

  return parts.map(p => {
    if (p.type === 'code') {
      const langClass = p.lang ? ` class="language-${escapeHtml(p.lang)}"` : '';
      return `<div class="code-wrap"><pre class="code-block"><code${langClass}>${escapeHtml(p.value)}</code></pre><button class="copy-btn">copy</button></div>`;
    }
    let s = escapeHtml(p.value);
    // Inline `code`
    s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    // **bold**
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    return s;
  }).join('');
}

// ── COMMAND PALETTE ──────────────────────────────────────────────────
function openCommandPalette() {
  State.cmdPaletteOpen = true;
  State.mode = 'SEARCH';
  document.getElementById('cmdOverlay').classList.add('open');
  setTimeout(() => {
    const input = document.getElementById('cmdInput');
    input.focus();
    input.select();
  }, 50);
  updateStatusbar();
}

function closeCommandPalette() {
  State.cmdPaletteOpen = false;
  State.mode = State.activeSession ? 'READ' : 'NORMAL';
  document.getElementById('cmdOverlay').classList.remove('open');
  document.getElementById('cmdInput').value = '';
  document.getElementById('cmdResults').innerHTML = '';
  State.cmdResults = [];
  State.cmdSelectedIndex = 0;
  updateStatusbar();
}

async function runCommandSearch(query) {
  if (!query) {
    document.getElementById('cmdResults').innerHTML = '';
    State.cmdResults = [];
    return;
  }
  const data = await apiFetch(`/api/search?q=${encodeURIComponent(query)}&limit=10`);
  if (!data) {
    document.getElementById('cmdResults').innerHTML = '<div class="cmd-empty">⚠ search failed</div>';
    return;
  }
  State.cmdResults = data.results || [];
  State.cmdSelectedIndex = 0;
  renderCommandResults();
}

function renderCommandResults() {
  const root = document.getElementById('cmdResults');
  if (!State.cmdResults.length) {
    root.innerHTML = '<div class="cmd-empty">no matches</div>';
    return;
  }
  root.innerHTML = State.cmdResults.map((r, i) => {
    // The /api/search snippet may already contain <mark> from FTS5;
    // sanitize the title separately and let snippet HTML through (it's our own server output).
    const title = highlightMatches(r.ai_title || '(untitled)', document.getElementById('cmdInput').value);
    const snippet = r.snippet || '';
    return `
      <div class="cmd-result ${i === State.cmdSelectedIndex ? 'selected' : ''}" data-index="${i}" data-uuid="${escapeHtml(r.session_uuid)}">
        <div class="cmd-r-title">${title}</div>
        <div class="cmd-r-meta">
          <span>${escapeHtml(r.project_name || '')}</span>
          ${r.git_branch ? '<span>' + escapeHtml(r.git_branch) + '</span>' : ''}
          ${r.duration_secs ? '<span>' + formatDuration(r.duration_secs) + '</span>' : ''}
          <span>${escapeHtml((r.started_at || '').slice(0, 10))}</span>
        </div>
        <div class="cmd-r-snippet">${snippet}</div>
      </div>
    `;
  }).join('');

  root.querySelectorAll('.cmd-result').forEach(el => {
    el.addEventListener('click', () => openSelectedResult(parseInt(el.dataset.index, 10)));
  });
}

function highlightMatches(text, query) {
  const safe = escapeHtml(text);
  if (!query) return safe;
  const tokens = query.split(/\s+/).filter(Boolean);
  let out = safe;
  for (const t of tokens) {
    const re = new RegExp('(' + t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
    out = out.replace(re, '<mark>$1</mark>');
  }
  return out;
}

function openSelectedResult(index) {
  const r = State.cmdResults[index];
  if (!r) return;
  closeCommandPalette();
  // Mark the corresponding inbox card if present, otherwise just load detail.
  const card = document.querySelector(`.session-card[data-uuid="${r.session_uuid}"]`);
  if (card) {
    document.querySelectorAll('.session-card').forEach(c => c.classList.remove('active'));
    card.classList.add('active');
    card.scrollIntoView({ block: 'nearest' });
  }
  loadDetail(r.session_uuid);
}

function moveCommandSelection(delta) {
  if (!State.cmdResults.length) return;
  State.cmdSelectedIndex = (State.cmdSelectedIndex + delta + State.cmdResults.length) % State.cmdResults.length;
  document.querySelectorAll('.cmd-result').forEach((el, i) => {
    el.classList.toggle('selected', i === State.cmdSelectedIndex);
    if (i === State.cmdSelectedIndex) el.scrollIntoView({ block: 'nearest' });
  });
}

// ── STATUSBAR ────────────────────────────────────────────────────────
function updateStatusbar() {
  document.getElementById('sbMode').textContent = State.mode;
  const total = State.stats ? State.stats.total_sessions : (State.inboxTotal || 0);
  document.getElementById('sbCount').textContent = `${total} session${total === 1 ? '' : 's'}`;

  const ctx = State.activeSession
    ? `${State.activeSession.project_name || ''} / ${State.activeSession.git_branch || '—'}`
    : (State.activeProject ? State.activeProject : 'all sessions');
  document.getElementById('sbContext').textContent = ctx;

  const sess = State.activeSession ? (State.activeSession.session_uuid || '').slice(0, 8) : '';
  document.getElementById('sbSession').textContent = sess;

  // /health gives no watch path, but we update sbWatch when connected
}

async function loadHealth() {
  const h = await apiFetch('/health');
  State.health = h;
  if (h) {
    document.getElementById('sbWatch').textContent = `watching ${(window.location.host || '').replace(/:\d+$/, '') || '—'}`;
  }
}

// ── EXPORT ───────────────────────────────────────────────────────────
async function exportSession(uuid) {
  try {
    const res = await fetch(`/api/sessions/${encodeURIComponent(uuid)}/export?format=markdown`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const cd = res.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="([^"]+)"/);
    a.download = m ? m[1] : `session-${uuid.slice(0, 8)}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    console.error('export failed', err);
    showToast('export failed', 'pink');
  }
}

// ── SETTINGS PANEL ───────────────────────────────────────────────────
async function openSettings() {
  State.activeSession = null;
  State.mode = 'NORMAL';
  document.querySelectorAll('.session-card').forEach(c => c.classList.remove('active'));
  updateStatusbar();

  const detail = document.getElementById('detail');
  detail.innerHTML = `
    <div class="settings">
      <div class="settings-header">⚙ settings</div>
      <div class="settings-section" id="settingsSources">
        <div class="settings-label">sources</div>
        <div style="color:var(--text-lo);font-size:11px">loading…</div>
      </div>
      <div class="settings-section" id="settingsVault">
        <div class="settings-label">vault</div>
      </div>
      <div class="settings-section" id="settingsServer">
        <div class="settings-label">server</div>
      </div>
      <div class="settings-section" id="settingsRedaction">
        <div class="settings-label">redaction</div>
      </div>
    </div>
  `;

  const [sourcesBody, statsBody, configBody] = await Promise.all([
    apiFetch('/api/config/sources'),
    apiFetch('/api/stats'),
    apiFetch('/api/config'),
  ]);

  renderSettingsSources(sourcesBody);
  renderSettingsVault(statsBody, configBody);
  renderSettingsServer(configBody);
  renderSettingsRedaction();
}

function renderSettingsSources(body) {
  const root = document.getElementById('settingsSources');
  if (!body || !Array.isArray(body.sources)) {
    root.innerHTML = '<div class="settings-label">sources</div><div class="error-state">⚠ failed to load</div>';
    return;
  }
  const rows = body.sources.map(s => {
    const status = s.enabled
      ? '<span class="src-on">● ON</span>'
      : '<span class="src-off">○ OFF</span>';
    const parserCell = s.parser_available
      ? '<span class="src-parser-ok">available</span>'
      : '<span class="src-parser-no">coming v2</span>';
    const action = s.parser_available
      ? `<button class="toggle-btn" data-id="${escapeHtml(s.id)}" data-enabled="${s.enabled}">${s.enabled ? 'disable' : 'enable'}</button>`
      : '<button class="toggle-btn" disabled>—</button>';
    return `
      <tr>
        <td><span class="src-id">${escapeHtml(s.id)}</span><br><span class="src-path">${escapeHtml(s.path)}</span></td>
        <td>${status}</td>
        <td>${parserCell}</td>
        <td>${action}</td>
      </tr>
    `;
  }).join('');

  root.innerHTML = `
    <div class="settings-label">sources</div>
    <table class="settings-table">
      <thead><tr><th>id / path</th><th>state</th><th>parser</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="settings-note">Changes take effect on next <code>vg start</code>. Only <code>claude_code</code> has a parser in v1.</div>
  `;

  root.querySelectorAll('.toggle-btn[data-id]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.id;
      const wasEnabled = btn.dataset.enabled === 'true';
      btn.disabled = true;
      try {
        const res = await fetch(`/api/config/sources/${encodeURIComponent(id)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: !wasEnabled }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        showToast(`source ${id}: ${wasEnabled ? 'disabled' : 'enabled'}`);
        // Reload the section
        const fresh = await apiFetch('/api/config/sources');
        renderSettingsSources(fresh);
      } catch (err) {
        console.error(err);
        showToast('toggle failed');
        btn.disabled = false;
      }
    });
  });
}

function renderSettingsVault(stats, config) {
  const root = document.getElementById('settingsVault');
  if (!stats || !config) {
    root.innerHTML = '<div class="settings-label">vault</div><div class="error-state">⚠</div>';
    return;
  }
  root.innerHTML = `
    <div class="settings-label">vault</div>
    <div class="kv">
      <span class="k">path</span>     <span class="v">${escapeHtml(config.vault_dir)}/vault.db</span>
      <span class="k">size</span>     <span class="v">${formatBytes(stats.db_size_bytes)}</span>
      <span class="k">sessions</span> <span class="v">${stats.total_sessions}</span>
      <span class="k">duration</span> <span class="v">${formatDuration(stats.total_duration_secs)}</span>
    </div>
  `;
}

function renderSettingsServer(config) {
  const root = document.getElementById('settingsServer');
  if (!config) {
    root.innerHTML = '<div class="settings-label">server</div><div class="error-state">⚠</div>';
    return;
  }
  root.innerHTML = `
    <div class="settings-label">server</div>
    <div class="kv">
      <span class="k">host</span> <span class="v">${escapeHtml(config.server_host)} <span style="color:var(--text-lo)">(localhost-only for security)</span></span>
      <span class="k">port</span> <span class="v">${config.server_port}</span>
      <span class="k">log level</span> <span class="v">${escapeHtml(config.log_level)}</span>
      <span class="k">debounce</span> <span class="v">${config.debounce_secs}s</span>
    </div>
  `;
}

function renderSettingsRedaction() {
  const root = document.getElementById('settingsRedaction');
  root.innerHTML = `
    <div class="settings-label">redaction</div>
    <div class="kv">
      <span class="k">status</span> <span class="v"><span class="src-on">● active</span> — patterns from defaults/redaction-rules.json</span>
      <span class="k">covered</span> <span class="v">api keys, AWS, kubeconfig, SSH, JWTs, db URLs, npm tokens, PEM blocks</span>
    </div>
  `;
}

// ── WEBSOCKET ────────────────────────────────────────────────────────
let ws = null;

function connectWebSocket() {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host || '127.0.0.1:7337';
  try {
    ws = new WebSocket(`${proto}//${host}/ws`);
  } catch (err) {
    console.error('ws connect failed', err);
    setTimeout(connectWebSocket, 2000);
    return;
  }

  ws.onopen = () => {
    document.getElementById('liveDot').classList.remove('disconnected');
  };

  ws.onmessage = ev => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === 'session_added' && msg.session) {
      handleNewSession(msg.session);
    }
  };

  ws.onclose = () => {
    document.getElementById('liveDot').classList.add('disconnected');
    setTimeout(connectWebSocket, 2000);
  };
}

function handleNewSession(session) {
  // Re-fetch the full row so the inbox card has all fields.
  apiFetch(`/api/sessions/${encodeURIComponent(session.session_uuid)}`).then(full => {
    if (!full) return;
    // Insert into inbox at top
    const fullRow = {
      session_uuid: full.session_uuid,
      project_name: full.project_name,
      git_branch: full.git_branch,
      ai_title: full.ai_title,
      started_at: full.started_at,
      duration_secs: full.duration_secs,
      has_subagents: full.has_subagents,
      tools_used: full.tools_used,
    };
    // Avoid duplicates
    if (!State.sessions.some(s => s.session_uuid === fullRow.session_uuid)) {
      State.sessions.unshift(fullRow);
      State.inboxTotal += 1;
      const list = document.getElementById('inboxList');
      const html = sessionCardHTML(fullRow);
      list.insertAdjacentHTML('afterbegin', html);
      const newCard = list.firstElementChild;
      newCard.classList.add('new');
      attachCardClickHandlers();
      document.getElementById('inboxCount').textContent = `${State.inboxTotal} backed up`;
    }
    // Refresh stats counters
    apiFetch('/api/stats').then(s => {
      State.stats = s;
      renderStats(s);
      updateStatusbar();
    });
    showToast(`⬡ new session: ${fullRow.project_name}/${fullRow.git_branch || ''}`);
  });
}

// ── TOAST ────────────────────────────────────────────────────────────
function showToast(text) {
  const el = document.getElementById('toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('show'), 3500);
}

// ── KEYBOARD SHORTCUTS ───────────────────────────────────────────────
function setupKeyboard() {
  document.addEventListener('keydown', e => {
    // ⌘K / Ctrl+K — open palette
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      openCommandPalette();
      return;
    }

    if (State.cmdPaletteOpen) {
      if (e.key === 'Escape') {
        e.preventDefault();
        closeCommandPalette();
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        moveCommandSelection(1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        moveCommandSelection(-1);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        openSelectedResult(State.cmdSelectedIndex);
      }
      return;
    }

    if (e.key === 'Escape') {
      // Clear active session
      State.activeSession = null;
      State.mode = 'NORMAL';
      document.querySelectorAll('.session-card').forEach(c => c.classList.remove('active'));
      const detail = document.getElementById('detail');
      detail.innerHTML = `
        <div class="welcome">
          <div class="welcome-logo">
            <span class="welcome-vim">vim</span><span class="welcome-gym">gym</span>
          </div>
          <div class="glow-line"></div>
          <div class="welcome-tagline">ai session memory</div>
          <div class="welcome-hint">Press <kbd>⌘K</kbd> to search · Click a session to read</div>
        </div>
      `;
      updateStatusbar();
    }
  });
}

// ── INIT ─────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Wire topbar search → command palette
  document.getElementById('topSearch').addEventListener('click', openCommandPalette);
  document.getElementById('settingsBtn').addEventListener('click', openSettings);

  // Wire command palette input
  const cmdInput = document.getElementById('cmdInput');
  cmdInput.addEventListener('input', e => {
    State.searchQuery = e.target.value;
    clearTimeout(State.cmdSearchTimer);
    State.cmdSearchTimer = setTimeout(() => runCommandSearch(State.searchQuery), 200);
  });

  // Click outside palette closes it
  document.getElementById('cmdOverlay').addEventListener('click', e => {
    if (e.target.id === 'cmdOverlay') closeCommandPalette();
  });

  setupKeyboard();
  setupInboxScroll();

  // Initial data load
  loadHealth();
  loadSidebar();
  loadInbox();

  // WebSocket live updates
  connectWebSocket();
});
