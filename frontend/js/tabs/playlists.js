/* Soundloom playlists tab — the save-everywhere hub.
   Import from Spotify/Deezer URLs, iTunes XML, M3U and CSV; export to
   M3U/CSV/iTunes XML/JSON files or push straight to Spotify/SoundCloud. */
import { API, showToast, esc, fmtDuration } from '../core.js';

const TARGETS = [
  { key: 'm3u', label: 'M3U playlist', desc: 'Opens in VLC, iTunes, foobar, everything' },
  { key: 'itunes_xml', label: 'iTunes XML', desc: 'File > Library > Import Playlist in Music/iTunes' },
  { key: 'csv', label: 'CSV', desc: 'Spreadsheet-friendly track list' },
  { key: 'json', label: 'JSON backup', desc: 'Lossless Soundloom backup' },
  { key: 'spotify', label: 'Spotify', desc: 'Creates the playlist on your account' },
  { key: 'soundcloud', label: 'SoundCloud', desc: 'Creates a set on your account' },
];

export const Playlists = {
  openId: null,

  async load() {
    if (this.openId) return this.openPlaylist(this.openId);
    try {
      const data = await API.get('/api/playlists');
      this.renderConnections();
      this.renderGrid(data.playlists || []);
    } catch (e) {
      showToast(e.message, 'error');
    }
  },

  // ---- connections (Spotify / SoundCloud OAuth) --------------------------
  async renderConnections() {
    const el = document.getElementById('connections-row');
    if (!el) return;
    let status = {};
    try { status = await API.get('/api/connections/status'); } catch { /* server older */ }
    const chip = (key, label) => {
      const s = status[key] || {};
      const cls = s.connected ? 'connected' : (s.configured ? 'ready' : 'unconfigured');
      const text = s.connected ? `${label} connected` : (s.configured ? `${label} ready — not signed in` : `${label} not set up`);
      const action = s.connected ? 'disconnect' : 'connect';
      return `
        <div class="conn-chip ${cls}">
          <span class="conn-dot"></span>
          <span class="conn-label">${esc(text)}</span>
          <button class="btn ghost sm" data-conn="${key}" data-action="${action}">
            ${s.connected ? 'Disconnect' : 'Connect'}
          </button>
        </div>`;
    };
    el.innerHTML = chip('spotify', 'Spotify') + chip('soundcloud', 'SoundCloud');
    el.querySelectorAll('[data-conn]').forEach(btn => {
      btn.addEventListener('click', () => {
        const platform = btn.dataset.conn;
        if (btn.dataset.action === 'connect') this.connect(platform);
        else this.disconnect(platform);
      });
    });
  },

  async connect(platform) {
    try {
      const r = await API.post('/api/connections/connect', { platform });
      window.open(r.auth_url, '_blank', 'width=520,height=760');
      showToast(`${platform === 'spotify' ? 'Spotify' : 'SoundCloud'} sign-in opened in a new tab — approve access there.`, 'info', 6000);
    } catch (e) {
      showToast(e.message, 'error', 7000);
    }
  },

  async disconnect(platform) {
    try {
      await API.post(`/api/connections/${platform}/disconnect`);
      showToast('Disconnected', 'info');
      this.renderConnections();
    } catch (e) { showToast(e.message, 'error'); }
  },

  // ---- grid ---------------------------------------------------------------
  renderGrid(playlists) {
    const el = document.getElementById('playlists-grid');
    if (!el) return;
    if (this.openId) { el.style.display = 'none'; return; }
    el.style.display = '';
    if (!playlists.length) {
      el.innerHTML = `
        <div class="empty-state">
          <svg viewBox="0 0 24 24" class="empty-icon"><path d="M3 6h13"/><path d="M3 12h13"/><path d="M3 18h9"/><path d="m16 15 5 3-5 3z"/></svg>
          <h3>No playlists yet</h3>
          <p>Import one from a Spotify/Deezer URL, an iTunes XML, an M3U or a CSV —
          then save it to any other platform or format.</p>
        </div>`;
      return;
    }
    el.innerHTML = playlists.map(p => `
      <div class="playlist-card" data-id="${p.id}">
        <div class="pl-card-title" title="${esc(p.name)}">${esc(p.name)}</div>
        <div class="pl-card-meta">${p.track_count} track${p.track_count === 1 ? '' : 's'}
          · ${p.matched_count} in library
          · ${esc(p.origin === 'manual' ? 'manual' : p.origin.replace('_url', ' import'))}</div>
        <div class="pl-card-bar"><span style="width:${p.track_count ? Math.round(100 * p.matched_count / p.track_count) : 0}%"></span></div>
        <div class="pl-card-actions">
          <button class="btn ghost sm" data-open="${p.id}">Open</button>
          <button class="btn ghost sm" data-export="${p.id}" title="Save to…">Save to…</button>
          <button class="btn danger sm" data-del="${p.id}" title="Delete playlist">✕</button>
        </div>
      </div>`).join('');
    el.querySelectorAll('[data-open]').forEach(b => b.addEventListener('click', () => this.openPlaylist(parseInt(b.dataset.open))));
    el.querySelectorAll('[data-export]').forEach(b => b.addEventListener('click', () => this.openExport(parseInt(b.dataset.export))));
    el.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', () => this.remove(parseInt(b.dataset.del))));
  },

  // ---- create / import ----------------------------------------------------
  showCreate() {
    document.getElementById('pl-create-card').style.display = '';
    document.getElementById('pl-new-name').focus();
  },
  hideCreate() {
    document.getElementById('pl-create-card').style.display = 'none';
    document.getElementById('pl-new-name').value = '';
    document.getElementById('pl-import-url').value = '';
    document.getElementById('pl-import-source').value = '';
    this.importSourceChanged();
  },
  importSourceChanged() {
    const v = document.getElementById('pl-import-source').value;
    document.getElementById('pl-url-row').style.display = v === 'url' ? '' : 'none';
    const hint = document.getElementById('pl-import-hint');
    if (v === 'itunes') hint.textContent = 'After creating, pick your Library.xml (iTunes: File > Library > Export Library; Music app: File > Library > Export Library).';
    else if (v === 'm3u') hint.textContent = 'After creating, pick your .m3u/.m3u8 file.';
    else if (v === 'csv') hint.textContent = 'After creating, pick a CSV with title and artist columns.';
    else hint.textContent = 'Files are chosen after creating (or use “Import file…” above to make a new playlist straight from a file).';
  },

  async create() {
    const nameEl = document.getElementById('pl-new-name');
    const name = nameEl.value.trim();
    const source = document.getElementById('pl-import-source').value;
    if (!name) { showToast('Give the playlist a name first', 'error'); return; }
    if (source === 'url') {
      const url = document.getElementById('pl-import-url').value.trim();
      if (!url) { showToast('Paste the playlist URL', 'error'); return; }
      try {
        const r = await API.post('/api/playlists/import/url', { url, name });
        showToast(`Imported ${r.tracks} tracks (${r.matched} already in your library)`, 'success', 5000);
        this.hideCreate();
        this.openPlaylist(r.id);
      } catch (e) { showToast(e.message, 'error', 7000); }
      return;
    }
    try {
      const r = await API.post('/api/playlists', { name });
      this.hideCreate();
      if (source) {
        this.openId = r.id;
        await this.openPlaylist(r.id, { skipLoad: true });
        this.pickFile(r.id, source);
      } else {
        showToast('Playlist created', 'success');
        this.openPlaylist(r.id);
      }
      this.load();
    } catch (e) { showToast(e.message, 'error'); }
  },

  // New playlist straight from a file (toolbar "Import file…").
  pickFileForNew() {
    const input = document.getElementById('pl-file-input');
    input.value = '';
    input.onchange = async () => {
      const file = input.files[0];
      if (!file) return;
      const base = file.name.replace(/\.(xml|m3u8?|csv)$/i, '');
      try {
        const created = await API.post('/api/playlists', { name: base });
        await this.sendFile(created.id, file);
        this.openPlaylist(created.id);
      } catch (e) { showToast(e.message, 'error', 7000); }
    };
    input.click();
  },

  // File picker for an existing playlist.
  pickFile(playlistId, kindHint) {
    const input = document.getElementById('pl-file-input');
    input.value = '';
    input.onchange = async () => {
      const file = input.files[0];
      if (!file) return;
      try {
        const r = await this.sendFile(playlistId, file, kindHint);
        showToast(`Imported ${r.added} tracks from ${file.name} (${r.matched} in library)`, 'success', 5000);
        this.openPlaylist(playlistId);
      } catch (e) { showToast(e.message, 'error', 7000); }
    };
    input.click();
  },

  async sendFile(playlistId, file, kind = '') {
    const buf = await file.arrayBuffer();
    const r = await fetch(`/api/playlists/import/file/${playlistId}?kind=${encodeURIComponent(kind)}`, {
      method: 'POST',
      headers: { 'X-Playlist-Filename': file.name },
      body: buf,
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `Import failed (${r.status})`);
    return j;
  },

  // ---- detail view --------------------------------------------------------
  async openPlaylist(id, { skipLoad = false } = {}) {
    this.openId = id;
    const grid = document.getElementById('playlists-grid');
    const detail = document.getElementById('playlist-detail');
    if (grid) grid.style.display = 'none';
    if (!detail) return;
    if (!skipLoad) {
      try {
        const data = await API.get(`/api/playlists/${id}`);
        this.renderDetail(data);
      } catch (e) {
        showToast(e.message, 'error');
        this.backToGrid();
      }
    }
  },

  renderDetail({ playlist, tracks }) {
    const el = document.getElementById('playlist-detail');
    el.style.display = '';
    const rows = (tracks || []).map(t => `
      <tr>
        <td class="muted">${t.position}</td>
        <td>${esc(t.title || t.path || '(untitled)')}${t.track_id ? '' : ' <span class="pl-unmatched" title="Not in your library yet">unmatched</span>'}</td>
        <td>${esc(t.artist)}</td>
        <td>${esc(t.album)}</td>
        <td class="muted">${t.duration ? fmtDuration(t.duration) : ''}</td>
        <td class="num"><button class="btn danger sm" data-rmrow="${t.id}" title="Remove from playlist">✕</button></td>
      </tr>`).join('');
    el.innerHTML = `
      <div class="panel-head">
        <div>
          <h1>${esc(playlist.name)}</h1>
          <p class="panel-sub">${tracks.length} track${tracks.length === 1 ? '' : 's'} ·
            ${tracks.filter(t => t.track_id).length} in your library ·
            imported from ${esc(playlist.origin === 'manual' ? 'scratch' : playlist.origin.replace('_url', ' '))}</p>
        </div>
        <div class="input-row">
          <button class="btn ghost sm" data-act="back">← All playlists</button>
          <button class="btn ghost sm" data-act="match">Re-scan matches</button>
          <button class="btn primary sm" data-act="enqueue" title="Queue downloads for tracks you don't have">Download missing</button>
          <button class="btn ghost sm" data-act="addfile">Import file into this…</button>
          <button class="btn ghost sm" data-act="export">Save to…</button>
        </div>
      </div>
      <div class="table-wrap">
        <table class="table">
          <thead><tr><th>#</th><th>Title</th><th>Artist</th><th>Album</th><th>⏱</th><th></th></tr></thead>
          <tbody>${rows || '<tr><td colspan="6" class="muted">Empty — import a file or download the missing ones.</td></tr>'}</tbody>
        </table>
      </div>`;
    el.querySelector('[data-act="back"]').addEventListener('click', () => this.backToGrid());
    el.querySelector('[data-act="match"]').addEventListener('click', () => this.rematch(playlist.id));
    el.querySelector('[data-act="enqueue"]').addEventListener('click', () => this.enqueue(playlist.id));
    el.querySelector('[data-act="addfile"]').addEventListener('click', () => this.pickFile(playlist.id));
    el.querySelector('[data-act="export"]').addEventListener('click', () => this.openExport(playlist.id));
    el.querySelectorAll('[data-rmrow]').forEach(b => b.addEventListener('click', () => this.removeRow(playlist.id, parseInt(b.dataset.rmrow))));
  },

  backToGrid() {
    this.openId = null;
    const detail = document.getElementById('playlist-detail');
    if (detail) { detail.style.display = 'none'; detail.innerHTML = ''; }
    this.load();
  },

  async rematch(id) {
    showToast('Re-scanning your library for matches…', 'info', 1500);
    try {
      const r = await API.post(`/api/playlists/${id}/match`, {});
      showToast(`${r.matched} of ${r.rows} tracks now match your library`, 'success');
      this.openPlaylist(id);
    } catch (e) { showToast(e.message, 'error'); }
  },

  async enqueue(id) {
    showToast('Queueing downloads for missing tracks…', 'info', 1500);
    try {
      const r = await API.post(`/api/playlists/${id}/enqueue`, { limit: 100 });
      showToast(r.enqueued
        ? `Queued ${r.enqueued} download${r.enqueued === 1 ? '' : 's'} (${r.unmatched_total} missing in total)`
        : `Nothing to queue — ${r.already_queued} already queued, rest are owned`,
        'success', 5000);
    } catch (e) { showToast(e.message, 'error'); }
  },

  async removeRow(playlistId, rowId) {
    try {
      await API.del(`/api/playlists/${playlistId}/tracks/${rowId}`);
      this.openPlaylist(playlistId);
    } catch (e) { showToast(e.message, 'error'); }
  },

  async remove(id) {
    if (!confirm('Delete this playlist? (Your downloaded music is not touched.)')) return;
    try {
      await API.del(`/api/playlists/${id}`);
      showToast('Playlist deleted', 'info');
      this.load();
    } catch (e) { showToast(e.message, 'error'); }
  },

  // ---- export -------------------------------------------------------------
  openExport(id) {
    let modal = document.getElementById('pl-export-modal');
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'pl-export-modal';
      modal.className = 'modal-backdrop';
      document.body.appendChild(modal);
    }
    modal.innerHTML = `
      <div class="modal-card">
        <div class="modal-head">
          <h3>Save playlist to…</h3>
          <button class="icon-btn" id="pl-export-close">✕</button>
        </div>
        <div class="export-grid">
          ${TARGETS.map(t => `
            <button class="export-option" data-target="${t.key}">
              <span class="export-name">${t.label}</span>
              <span class="export-desc">${t.desc}</span>
            </button>`).join('')}
        </div>
        <p class="hint">Spotify and SoundCloud create the playlist on your account — connect once from this page. File exports land in <code>data/exports/</code>.</p>
      </div>`;
    modal.style.display = '';
    document.getElementById('pl-export-close').addEventListener('click', () => { modal.style.display = 'none'; });
    modal.addEventListener('click', (e) => { if (e.target === modal) modal.style.display = 'none'; });
    modal.querySelectorAll('[data-target]').forEach(btn => {
      btn.addEventListener('click', () => this.exportTo(id, btn.dataset.target, modal));
    });
  },

  async exportTo(id, target, modal) {
    showToast(target === 'spotify' || target === 'soundcloud'
      ? `Creating on ${target === 'spotify' ? 'Spotify' : 'SoundCloud'}… (this can take a minute for big playlists)`
      : 'Writing file…', 'info', 2500);
    try {
      const r = await API.post(`/api/playlists/${id}/export`, { target });
      if (r.path) {
        showToast(`Saved: ${r.path} (${r.tracks} tracks)`, 'success', 7000);
      } else {
        const where = r.url ? ` — <a href="${esc(r.url)}" target="_blank" rel="noopener">open it</a>` : '';
        showToast(`Created on ${target}${where}`, 'success', 8000);
        if (r.missing > 0) {
          showToast(`${r.missing} track${r.missing === 1 ? '' : 's'} could not be found on ${target}`, 'error', 7000);
        }
        this.renderConnections();
      }
      modal.style.display = 'none';
    } catch (e) {
      showToast(e.message, 'error', 8000);
      if (String(e.message).toLowerCase().includes('not connected')) {
        modal.style.display = 'none';
        this.renderConnections();
      }
    }
  },
};
