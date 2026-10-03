/* Soundloom core — API client, event bus, shared state and utilities. */

// --- API client ---
export const API = {
  _parseError(r, text) {
    try { const j = JSON.parse(text); return j.detail || text; } catch { return text; }
  },
  async get(url) { const r = await fetch(url); if (!r.ok) throw new Error(this._parseError(r, await r.text())); return r.json(); },
  async post(url, data) {
    const r = await fetch(url, data === undefined ? { method: 'POST' } : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
    if (!r.ok) throw new Error(this._parseError(r, await r.text()));
    return r.json();
  },
  async put(url, data) {
    const r = await fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
    if (!r.ok) throw new Error(this._parseError(r, await r.text()));
    return r.json();
  },
  async del(url) { const r = await fetch(url, { method: 'DELETE' }); if (!r.ok) throw new Error(this._parseError(r, await r.text())); return r.json(); },
};

// --- State ---
const _state = { theme: 'dark', currentTab: 'library' };
export const State = {
  get: (k) => _state[k],
  set: (k, v) => { _state[k] = v; },
};

// --- Event bus (WebSocket with reconnect backoff) ---
const handlers = new Map();

export const Bus = {
  ws: null,
  reconnectTimer: null,
  attempts: 0,

  on(type, fn) {
    if (!handlers.has(type)) handlers.set(type, []);
    handlers.get(type).push(fn);
  },

  connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.ws = new WebSocket(`${proto}//${location.host}/ws/events`);
    this.ws.onopen = () => {
      this.attempts = 0;
      setWsDot(true);
    };
    this.ws.onmessage = (e) => this.dispatch(e);
    this.ws.onclose = () => {
      setWsDot(false);
      this.scheduleReconnect();
    };
    this.ws.onerror = () => this.ws.close();
  },

  scheduleReconnect() {
    if (this.reconnectTimer) return;
    // Exponential backoff: 1s, 1.5s, 2.25s … capped at 15s.
    const delay = Math.min(1000 * Math.pow(1.5, this.attempts), 15000);
    this.attempts += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  },

  dispatch(e) {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    for (const fn of handlers.get(msg.type) || []) {
      try { fn(msg.data); } catch (err) { console.error(`Handler for ${msg.type} failed:`, err); }
    }
  },
};

function setWsDot(connected) {
  const dot = document.getElementById('ws-dot');
  if (dot) {
    dot.classList.toggle('off', !connected);
    dot.title = connected ? 'Live connection' : 'Reconnecting…';
  }
}

// --- Toast ---
export function showToast(msg, type = 'info', dur = 3500) {
  const c = document.getElementById('toast-container');
  if (!c) return;
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { t.classList.add('toast-fadeout'); setTimeout(() => t.remove(), 300); }, dur);
}

// --- Utilities ---
export function esc(s) {
  if (s === null || s === undefined) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

export function fmtDuration(s) {
  if (!s || s <= 0) return '0:00';
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
}

export function fmtSize(b) {
  if (!b) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0; let s = b;
  while (s >= 1024 && i < units.length - 1) { s /= 1024; i++; }
  return s.toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function isURL(s) {
  try { new URL(s); return true; } catch { return false; }
}

export function coverImg(track, cls = 'track-cover') {
  // Cover art is served by /api/library/tracks/{id}/cover, which extracts
  // embedded art on demand and falls back to the app placeholder — so a
  // plain <img> is always safe. Lazy loading keeps big grids cheap.
  const id = typeof track === 'object' ? (track.id ?? track.cover_id) : track;
  return `<img class="${cls}" src="/api/library/tracks/${id}/cover" alt="" loading="lazy" onerror="this.src='/static/img/logo.svg'">`;
}

// --- Artist autocomplete ---------------------------------------------------
// Shared dropdown fed by /api/library/artists?search= (the user's own
// library). One open dropdown at a time; closes on outside click and Escape.

let _acOpen = null;
let _acSeq = 0;

function _acClose() {
  if (_acOpen) { _acOpen.remove(); _acOpen = null; }
}

function _acSet(input, value, apply) {
  input.value = value;
  _acClose();
  input.focus();
  if (apply) apply(value);
}

export function attachArtistAutocomplete(input, { onSelect, onEnter } = {}) {
  if (!input) return;
  const DELAY = 180;

  input.setAttribute('autocomplete', 'off');
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-autocomplete', 'list');

  input.addEventListener('input', debounce(async () => {
    const q = input.value.trim();
    _acClose();
    if (q.length < 2) return;
    const seq = ++_acSeq;
    let artists = [];
    try {
      const data = await API.get(`/api/library/artists?search=${encodeURIComponent(q)}`);
      artists = data.slice(0, 8);
    } catch { return; }
    if (seq !== _acSeq) return;              // a newer keystroke already answered
    if (!artists.length) return;

    const box = document.createElement('div');
    box.className = 'autocomplete';
    box.innerHTML = artists.map(a => `
      <div class="ac-item" role="option" data-name="${esc(a.name)}">
        <span class="ac-name">${esc(a.name)}</span>
        <span class="ac-count">${a.track_count} track${a.track_count === 1 ? '' : 's'}</span>
      </div>`).join('')
      + '<div class="ac-foot">from your library</div>';

    box.querySelectorAll('.ac-item').forEach(item => {
      item.addEventListener('mousedown', (e) => {
        e.preventDefault();                    // keep focus in the input
        _acSet(input, item.dataset.name, onSelect);
      });
    });

    document.body.appendChild(box);
    const r = input.getBoundingClientRect();
    box.style.left = r.left + 'px';
    box.style.top = (r.bottom + 4) + 'px';
    box.style.minWidth = r.width + 'px';
    box.style.zIndex = 1000;
    _acOpen = box;
  }, DELAY));

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _acOpen) { _acClose(); return; }
    if (_acOpen) {
      const items = [..._acOpen.querySelectorAll('.ac-item')];
      const idx = items.findIndex(i => i.classList.contains('hover'));
      if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && items.length) {
        e.preventDefault();
        items.forEach(i => i.classList.remove('hover'));
        const next = e.key === 'ArrowDown' ? Math.min(idx + 1, items.length - 1) : Math.max(idx - 1, 0);
        items[next < 0 ? 0 : next].classList.add('hover');
        return;
      }
      if (e.key === 'Enter' && idx >= 0) {
        e.preventDefault();
        _acSet(input, items[idx].dataset.name, onSelect);
        return;
      }
    }
    if (e.key === 'Enter' && onEnter) onEnter();
  });

  input.addEventListener('blur', () => setTimeout(_acClose, 120));
}

document.addEventListener('mousedown', (e) => {
  if (_acOpen && !_acOpen.contains(e.target) && e.target !== _acOpen) _acClose();
});
