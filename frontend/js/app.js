/* ===== Big Pickle - VividlyMusicaly Frontend ===== */

// --- API Client ---
const API = {
  _parseError(r, text) {
    try { const j = JSON.parse(text); return j.detail || text; } catch { return text; }
  },
  async get(url) { const r = await fetch(url); if (!r.ok) throw new Error(this._parseError(r, await r.text())); return r.json(); },
  async post(url, data) { const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }); if (!r.ok) throw new Error(this._parseError(r, await r.text())); return r.json(); },
  async put(url, data) { const r = await fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }); if (!r.ok) throw new Error(this._parseError(r, await r.text())); return r.json(); },
  async del(url) { const r = await fetch(url, { method: 'DELETE' }); if (!r.ok) throw new Error(this._parseError(r, await r.text())); return r.json(); },
};

// --- WebSocket Event Bus ---
const EventBus = {
  ws: null, reconnectTimer: null,

  connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.ws = new WebSocket(`${proto}//${location.host}/ws/events`);
    this.ws.onmessage = (e) => this.onMessage(e);
    this.ws.onclose = () => this.scheduleReconnect();
    this.ws.onerror = () => this.ws.close();
  },

  scheduleReconnect() {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; this.connect(); }, 3000);
  },

  onMessage(e) {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'job_update') this.onJobUpdate(msg.data);
      else if (msg.type === 'library_change') this.onLibraryChange(msg.data);
      else if (msg.type === 'log') this.onLog(msg.data);
    } catch {}
  },

  onJobUpdate(d) {
    if (State.get('currentTab') === 'queue') Queue.load();
    Queue.updateBadge();
    if (d.status === 'complete') showToast(`Downloaded: ${d.output_path || ''}`, 'success');
    else if (d.status === 'failed') showToast(`Failed: ${d.error || 'unknown error'}`, 'error');
  },

  onLibraryChange(d) {
    if (State.get('currentTab') === 'library') { Library.loadTracks(); Library.loadStats(); }
  },

  onLog(d) {
    if (State.get('currentTab') === 'logs') Logs.refresh();
  },
};

// --- Toast ---
function showToast(msg, type = 'info', dur = 3500) {
  const c = document.getElementById('toast-container');
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { t.classList.add('toast-fadeout'); setTimeout(() => t.remove(), 250); }, dur);
}

// --- Utilities ---
function fmtDuration(s) {
  if (!s || s <= 0) return '0:00';
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
}
function fmtSize(b) {
  if (!b) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0; let s = b;
  while (s >= 1024 && i < units.length - 1) { s /= 1024; i++; }
  return s.toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function isURL(s) { try { new URL(s); return true; } catch { return false; } }
function switchTab(tab) {
  const nav = document.querySelector(`.nav-item[data-tab="${tab}"]`);
  if (nav) nav.click();
}

// --- State ---
const State = { _data: { theme: 'dark', currentTab: 'library' } };
State.get = (k) => State._data[k];
State.set = (k, v) => { State._data[k] = v; };

// --- Library Module ---
const Library = {
  tracks: [], total: 0, page: 1, perPage: 100, sortBy: 'title', order: 'ASC',
  subView: 'tracks', _albumFilter: '',

  async loadTracks() {
    const search = document.getElementById('lib-search')?.value || '';
    const artist = document.getElementById('lib-artist-filter')?.value || '';
    const album = this._albumFilter || '';
    const offset = (this.page - 1) * this.perPage;
    try {
      const params = new URLSearchParams({
        sort: this.sortBy, order: this.order, search, artist, album,
        limit: this.perPage, offset
      });
      const data = await API.get(`/api/library/tracks?${params}`);
      this.tracks = data.tracks || [];
      this.total = data.total || 0;
      this.renderTracks();
      this.renderPagination();
    } catch (e) { console.error('Failed to load tracks:', e); }
  },

  renderTracks() {
    const tbody = document.getElementById('tracks-body');
    const empty = document.getElementById('lib-empty');
    const view = document.getElementById('lib-tracks-view');
    if (!this.tracks.length) {
      empty.style.display = '';
      view.style.display = 'none';
      return;
    }
    empty.style.display = 'none';
    view.style.display = '';
    tbody.innerHTML = this.tracks.map(t => `
      <tr data-track-id="${t.id}">
        <td><div class="track-cover">${t.cover_art_path ? `<img src="/static/covers/${t.id}.jpg" onerror="this.parentElement.textContent='&#9835;'">` : '&#9835;'}</div></td>
        <td>${esc(t.title)}</td>
        <td>${esc(t.artist)}</td>
        <td>${esc(t.album)}</td>
        <td>${fmtDuration(t.duration)}</td>
        <td><span class="tag">${(t.format || '').toUpperCase()}</span></td>
        <td>${fmtSize(t.file_size)}</td>
      </tr>
    `).join('');
    tbody.querySelectorAll('tr[data-track-id]').forEach(row => {
      row.addEventListener('dblclick', () => Metadata.editTrack(parseInt(row.dataset.trackId)));
    });
  },

  renderPagination() {
    const el = document.getElementById('tracks-pagination');
    const pages = Math.ceil(this.total / this.perPage);
    if (pages <= 1) { el.innerHTML = ''; return; }
    let html = '';
    for (let i = 1; i <= pages && i <= 20; i++) {
      html += `<button class="page-btn ${i === this.page ? 'active' : ''}" onclick="Library.goPage(${i})">${i}</button>`;
    }
    el.innerHTML = html;
  },

  goPage(p) { this.page = p; this.loadTracks(); },

  sort(col) {
    if (this.sortBy === col) { this.order = this.order === 'ASC' ? 'DESC' : 'ASC'; }
    else { this.sortBy = col; this.order = 'ASC'; }
    this.page = 1;
    document.querySelectorAll('#tracks-table th').forEach(th => { th.classList.remove('sorted-asc', 'sorted-desc'); });
    const th = document.querySelector(`th[data-sort="${col}"]`);
    if (th) th.classList.add(this.order === 'ASC' ? 'sorted-asc' : 'sorted-desc');
    this.loadTracks();
  },

  switchView(view) {
    this.subView = view;
    document.getElementById('lib-tracks-view').style.display = 'none';
    document.getElementById('lib-artists-view').style.display = 'none';
    document.getElementById('lib-albums-view').style.display = 'none';
    if (view === 'tracks') { document.getElementById('lib-tracks-view').style.display = ''; this.loadTracks(); }
    else if (view === 'artists') { document.getElementById('lib-artists-view').style.display = ''; this.loadArtists(); }
    else if (view === 'albums') { document.getElementById('lib-albums-view').style.display = ''; this.loadAlbums(); }
  },

  async loadArtists() {
    try {
      const data = await API.get('/api/library/artists');
      const el = document.getElementById('lib-artists-view');
      const filter = document.getElementById('lib-artist-filter');
      filter.innerHTML = '<option value="">All Artists</option>' + data.map(a => `<option value="${esc(a.name)}">${esc(a.name)} (${a.track_count})</option>`).join('');
      el.innerHTML = data.length ? data.map(a => `
        <div class="artist-row" data-artist="${esc(a.name)}">
          <span class="artist-name">${esc(a.name)}</span>
          <span class="artist-count">${a.track_count} tracks</span>
          <button class="btn btn-sm" style="margin-left:auto;font-size:11px;padding:3px 8px;" onclick="event.stopPropagation(); Library.searchArtistWeb('${esc(a.name)}')">Search Web</button>
        </div>
      `).join('') : '<div class="empty-state"><p>No artists found</p></div>';
      el.querySelectorAll('.artist-row').forEach(row => {
        row.addEventListener('click', () => Library.filterArtist(row.dataset.artist));
      });
    } catch (e) { console.error(e); }
  },

  async loadAlbums() {
    try {
      const data = await API.get('/api/library/albums');
      const el = document.getElementById('lib-albums-view');
      el.innerHTML = data.length ? `<div class="album-grid">${data.map(a => `
        <div class="album-card" data-album="${esc(a.name)}">
          <div class="album-cover">&#9835;</div>
          <div class="album-card-title">${esc(a.name)}</div>
          <div class="album-card-meta">${esc(a.artist || 'Unknown')} &middot; ${a.track_count} tracks</div>
        </div>
      `).join('')}</div>` : '<div class="empty-state"><p>No albums found</p></div>';
      el.querySelectorAll('.album-card').forEach(card => {
        card.addEventListener('click', () => Library.filterAlbum(card.dataset.album));
      });
    } catch (e) { console.error(e); }
  },

  filterArtist(name) {
    document.getElementById('lib-sub-tab').value = 'tracks';
    document.getElementById('lib-artist-filter').value = name;
    this.switchView('tracks');
  },

  searchArtistWeb(name) {
    document.getElementById('add-url').value = name;
    switchTab('add');
    setTimeout(() => AddSource.preview(), 100);
  },

  filterAlbum(name) {
    this._albumFilter = name;
    document.getElementById('lib-sub-tab').value = 'tracks';
    this.switchView('tracks');
  },

  debounceSearch: debounce(function() { Library.page = 1; Library.loadTracks(); }, 300),

  async scan() {
    showToast('Scanning library...', 'info');
    document.getElementById('lib-progress').style.display = '';
    try {
      await API.get('/api/library/scan');
      this.pollScan();
    } catch (e) { showToast('Scan failed: ' + e.message, 'error'); }
  },

  async pollScan() {
    try {
      const s = await API.get('/api/library/scan/status');
      const pct = s.total > 0 ? Math.round((s.processed / s.total) * 100) : 0;
      document.getElementById('scan-progress-fill').style.width = pct + '%';
      document.getElementById('scan-status-text').textContent = `Scanning: ${s.processed}/${s.total} (${s.errors} errors)`;
      if (s.scanning) { setTimeout(() => Library.pollScan(), 500); }
      else {
        document.getElementById('lib-progress').style.display = 'none';
        showToast(`Scan complete: ${s.processed} files indexed`, 'success');
        this.loadTracks();
        this.loadStats();
      }
    } catch (e) { console.error(e); }
  },

  async loadStats() {
    try {
      const s = await API.get('/api/library/stats');
      document.getElementById('status-tracks').textContent = s.total_tracks + ' tracks';
      document.getElementById('status-artists').textContent = s.total_artists + ' artists';
      document.getElementById('status-albums').textContent = s.total_albums + ' albums';
    } catch (e) {}
  }
};

// --- Queue Module ---
const Queue = {
  jobs: [], currentTab: 'active', pollTimer: null,

  switchTab(tab) {
    this.currentTab = tab;
    document.querySelectorAll('.queue-tabs .tab-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.tab === tab);
    });
    this.load();
  },

  async load() {
    try {
      const statusMap = { active: 'downloading', completed: 'complete', failed: 'failed' };
      const data = await API.get(`/api/queue?status=${statusMap[this.currentTab] || ''}`);
      this.jobs = data.jobs || [];
      this.render();
    } catch (e) { console.error(e); }
  },

  render() {
    const el = document.getElementById('queue-list');
    if (!this.jobs.length) {
      const msgs = { active: 'No active downloads', completed: 'No completed downloads', failed: 'No failed downloads' };
      el.innerHTML = `<div class="empty-state"><div class="empty-icon">&#128229;</div><p>${msgs[this.currentTab]}</p></div>`;
      return;
    }
    el.innerHTML = this.jobs.map(j => `
      <div class="job-card">
        <div class="job-status ${j.status}"></div>
        <div class="job-info">
          <div class="job-title">${esc(j.title || j.query || j.source_url || 'Unknown')}</div>
          <div class="job-meta">${esc(j.artist || '')} &middot; ${j.output_format.toUpperCase()} &middot; ${j.status} ${j.error ? ' - ' + esc(j.error.substring(0,80)) : ''}</div>
        </div>
        <div class="job-progress"><div class="progress-bar"><div class="progress-fill" style="width:${j.progress || 0}%"></div></div></div>
        <div class="job-actions">
          ${j.status === 'failed' ? `<button class="btn btn-sm btn-secondary" onclick="Queue.retry(${j.id})">Retry</button>` : ''}
          <button class="btn btn-sm btn-danger" onclick="Queue.remove(${j.id})">&#10005;</button>
        </div>
      </div>
    `).join('');
  },

  startPolling() {
    this.stopPolling();
    this.pollTimer = setInterval(() => {
      if (State.get('currentTab') === 'queue') { this.load(); this.updateBadge(); }
    }, 2000);
  },
  stopPolling() { if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; } },

  async retry(id) { try { await API.post(`/api/queue/${id}/retry`); showToast('Retrying...', 'info'); this.load(); } catch (e) { showToast(e.message, 'error'); } },
  async remove(id) { try { await API.del(`/api/queue/${id}`); this.load(); } catch (e) { showToast(e.message, 'error'); } },
  async clearCompleted() { try { await API.post('/api/queue/clear-completed'); this.load(); } catch (e) {} },
  async clearFailed() { try { await API.post('/api/queue/clear-failed'); this.load(); } catch (e) {} },

  async updateBadge() {
    try {
      const s = await API.get('/api/queue/stats');
      const total = s.active + s.pending;
      const badge = document.getElementById('queue-badge');
      if (total > 0) { badge.textContent = total; badge.style.display = ''; }
      else { badge.style.display = 'none'; }
      document.getElementById('status-queue').textContent = `Queue: ${s.active} active, ${s.pending} pending`;
    } catch (e) {}
  }
};

// --- Add Source Module ---
const AddSource = {
  plan: null,

  async preview() {
    const input = document.getElementById('add-url');
    const url = input.value.trim();
    if (!url) return;

    const format = document.getElementById('add-format').value;
    const quality = document.getElementById('add-quality').value;
    const previewDiv = document.getElementById('add-preview');
    const resultsDiv = document.getElementById('add-results');
    const btn = document.getElementById('add-submit-btn');

    if (isURL(url)) {
      await this.previewUrl(url, format, quality, previewDiv, resultsDiv, btn);
    } else {
      await this.search(url, resultsDiv, previewDiv, btn);
    }
  },

  async search(query, resultsDiv, previewDiv, btn) {
    btn.disabled = true;
    btn.textContent = 'Searching...';
    previewDiv.style.display = 'none';
    resultsDiv.style.display = '';

    try {
      const [localData, webData] = await Promise.all([
        API.get(`/api/library/tracks?search=${encodeURIComponent(query)}&limit=10`).catch(() => ({ tracks: [] })),
        API.post('/api/sources/search-web', { query }).catch(() => ({ results: [] })),
      ]);
      const tracks = localData.tracks || [];
      const webResults = webData.results || [];

      const list = document.getElementById('add-results-list');
      if (!tracks.length && !webResults.length) {
        list.innerHTML = '<div class="text-muted" style="padding:12px;">No results found. Try a URL instead.</div>';
        btn.textContent = 'Search';
        btn.disabled = false;
        return;
      }

      let html = '';
      if (tracks.length) {
        html += '<div style="font-size:12px;font-weight:600;color:var(--text-muted);margin-bottom:8px;">IN YOUR LIBRARY</div>';
        html += tracks.map(t => `
          <div class="job-card" style="cursor:default;">
            <div class="job-info">
              <div class="job-title">${esc(t.title || 'Unknown')}</div>
              <div class="job-meta">${esc(t.artist || '')} ${t.album ? '&middot; ' + esc(t.album) : ''} ${t.duration ? '&middot; ' + fmtDuration(t.duration) : ''}</div>
            </div>
            <div class="job-actions"><span class="text-muted" style="font-size:11px;">Already in library</span></div>
          </div>
        `).join('');
      }

      if (webResults.length) {
        const srcIcons = { youtube: '&#9654;', soundcloud: '&#9835;', musicbrainz: '&#9881;', spotify: '&#9836;', unknown: '&#8250;' };
        html += '<div style="font-size:12px;font-weight:600;color:var(--text-muted);margin:12px 0 8px;">ONLINE RESULTS</div>';
        html += webResults.map((r, i) => `
          <div class="job-card" style="cursor:pointer;" onclick="AddSource.previewWebResult(${i})">
            <div style="display:flex;gap:10px;align-items:flex-start;flex:1;min-width:0;">
              ${r.thumbnail_url ? `<img src="${esc(r.thumbnail_url)}" style="width:48px;height:48px;border-radius:6px;object-fit:cover;flex-shrink:0;">` : ''}
              <div class="job-info" style="min-width:0;">
                <div class="job-title">${esc(r.title || 'Unknown')}</div>
                <div class="job-meta">${esc(r.artist || '')} ${r.album ? '&middot; ' + esc(r.album) : ''} ${r.duration ? '&middot; ' + fmtDuration(r.duration) : ''}</div>
                <div class="job-meta" style="font-size:11px;">${srcIcons[r.source] || ''} ${esc(r.source)} ${r.confidence ? '&middot; ' + Math.round(r.confidence) + '%' : ''}</div>
              </div>
            </div>
            <div class="job-actions">
              <button class="btn btn-sm btn-primary" onclick="event.stopPropagation(); AddSource.previewWebResult(${i})">Preview</button>
            </div>
          </div>
        `).join('');

        AddSource._webResults = webResults;
      }

      list.innerHTML = html;
      btn.textContent = 'Search';
      btn.disabled = false;
    } catch (e) {
      showToast('Search failed: ' + e.message, 'error');
      resultsDiv.style.display = 'none';
      btn.textContent = 'Search';
      btn.disabled = false;
    }
  },

  async previewWebResult(index) {
    const r = (this._webResults || [])[index];
    if (!r || !r.source_url) { showToast('No URL for this result', 'error'); return; }
    const url = r.source_url;
    const input = document.getElementById('add-url');
    input.value = url;
    const format = document.getElementById('add-format').value;
    const quality = document.getElementById('add-quality').value;
    const previewDiv = document.getElementById('add-preview');
    const resultsDiv = document.getElementById('add-results');
    const btn = document.getElementById('add-submit-btn');
    await this.previewUrl(url, format, quality, previewDiv, resultsDiv, btn);
  },

  async previewUrl(url, format, quality, previewDiv, resultsDiv, btn) {
    btn.disabled = true;
    btn.textContent = 'Resolving...';
    resultsDiv.style.display = 'none';
    previewDiv.style.display = '';

    try {
      const plan = await API.post('/api/sources/preview', { url, format, quality });
      this.plan = plan;
      const confColor = plan.match_confidence >= 85 ? 'var(--accent)' : plan.match_confidence >= 70 ? '#f0ad4e' : '#d9534f';
      previewDiv.querySelector('#add-preview-card').innerHTML = `
        <div style="display:flex;gap:14px;align-items:flex-start;">
          ${plan.thumbnail ? `<img src="${esc(plan.thumbnail)}" style="width:80px;height:80px;border-radius:8px;object-fit:cover;flex-shrink:0;">` : ''}
          <div style="flex:1;min-width:0;">
            <div style="font-weight:600;font-size:15px;">${esc(plan.title)}</div>
            <div class="text-muted">${esc(plan.display_artist || plan.artist)}</div>
            <div class="text-muted" style="font-size:12px;margin-top:4px;">
              ${plan.duration ? fmtDuration(plan.duration) + ' &middot; ' : ''}
              ${plan.output_format.toUpperCase()} &middot; ${plan.bitrate || plan.quality_profile}
            </div>
            <div style="margin-top:6px;">
              <span style="color:${confColor};font-weight:600;font-size:12px;">Match: ${plan.match_confidence}%</span>
              <span class="text-muted" style="font-size:12px;margin-left:6px;">${plan.match_explanation}</span>
            </div>
            ${plan.match_warnings.length ? `<div style="color:#f0ad4e;font-size:11px;margin-top:2px;">${plan.match_warnings.map(w => esc(w)).join(' &middot; ')}</div>` : ''}
            <div style="margin-top:6px;font-size:11px;color:var(--text-muted);">
              <strong>Destination:</strong> ${esc(plan.destination_path)}
            </div>
          </div>
        </div>`;
      btn.textContent = 'Add to Queue';
      btn.disabled = false;
      btn.onclick = () => this.confirmDownload();
    } catch (e) {
      showToast('Failed to resolve: ' + e.message, 'error');
      previewDiv.style.display = 'none';
      btn.textContent = 'Preview';
      btn.disabled = false;
      btn.onclick = () => this.preview();
    }
  },

  async confirmDownload() {
    if (!this.plan) return;
    const btn = document.getElementById('add-submit-btn');
    btn.disabled = true;
    btn.textContent = 'Adding...';
    try {
      await API.post('/api/sources/confirm', this.plan);
      showToast('Added to download queue', 'success');
      document.getElementById('add-url').value = '';
      document.getElementById('add-preview').style.display = 'none';
      btn.textContent = 'Preview';
      btn.disabled = false;
      btn.onclick = () => this.preview();
      this.plan = null;
      Queue.load();
      Queue.updateBadge();
    } catch (e) {
      showToast('Failed to add: ' + e.message, 'error');
      btn.textContent = 'Confirm & Download';
      btn.disabled = false;
    }
  },

  cancelPreview() {
    this.plan = null;
    document.getElementById('add-preview').style.display = 'none';
    const btn = document.getElementById('add-submit-btn');
    btn.textContent = 'Preview';
    btn.disabled = false;
    btn.onclick = () => this.preview();
  },
};

// --- Watch Module ---
const Watch = {
  async add() {
    const input = document.getElementById('watch-artist-input');
    const name = input.value.trim();
    if (!name) return;
    try {
      await API.post('/api/watch', { artist_name: name });
      showToast(`Now watching: ${name}`, 'success');
      input.value = '';
      this.load();
    } catch (e) { showToast(e.message, 'error'); }
  },

  async load() {
    try {
      const data = await API.get('/api/watch');
      const el = document.getElementById('watch-list');
      if (!data.length) { el.innerHTML = '<div class="empty-state"><div class="empty-icon">&#128065;</div><p>No artists being watched</p></div>'; return; }
      el.innerHTML = data.map(w => `
        <div class="watch-item">
          <div class="watch-info">
            <strong>${esc(w.artist_name)}</strong>
            <div class="text-muted">Last checked: ${w.last_checked || 'Never'}</div>
          </div>
          <div class="watch-actions">
            <label class="toggle-switch" title="Auto-download new releases">
              <input type="checkbox" ${w.auto_download ? 'checked' : ''} onchange="Watch.toggleAuto(${w.id}, this.checked)">
              <span class="toggle-slider"></span>
            </label>
            <button class="btn btn-sm btn-secondary" disabled title="Coming soon: auto-check for new releases" style="opacity:0.5;">Check Now</button>
            <button class="btn btn-sm btn-danger" onclick="Watch.remove(${w.id})">&#10005;</button>
          </div>
        </div>
      `).join('');
    } catch (e) { console.error(e); }
  },

  async toggleAuto(id, val) { try { await API.put(`/api/watch/${id}`, { auto_download: val ? 1 : 0 }); } catch (e) {} },
  async check(id) { showToast('Checking for new releases...', 'info'); },
  async remove(id) { try { await API.del(`/api/watch/${id}`); this.load(); showToast('Removed from watch list', 'info'); } catch (e) {} }
};

// --- Metadata Module ---
const Metadata = {
  currentTrack: null,

  searchDebounce: debounce(async function() {
    const q = document.getElementById('meta-search').value.trim();
    if (!q) { document.getElementById('meta-results').style.display = 'none'; return; }
    try {
      const data = await API.get(`/api/library/tracks?search=${encodeURIComponent(q)}&limit=10`);
      const el = document.getElementById('meta-results');
      if (!data.tracks.length) { el.innerHTML = '<p class="text-muted">No matches</p>'; el.style.display = ''; return; }
      el.innerHTML = data.tracks.map(t => `
        <div class="artist-row" data-track-id="${t.id}">
          <span class="artist-name">${esc(t.title)}</span>
          <span class="artist-count">${esc(t.artist)}</span>
        </div>
      `).join('');
      el.querySelectorAll('.artist-row').forEach(row => {
        row.addEventListener('click', () => Metadata.editTrack(parseInt(row.dataset.trackId)));
      });
      el.style.display = '';
    } catch (e) { console.error(e); }
  }, 300),

  async editTrack(id) {
    try {
      const t = await API.get(`/api/library/tracks/${id}`);
      this.currentTrack = t;
      document.getElementById('meta-title').value = t.title || '';
      document.getElementById('meta-artist').value = t.artist || '';
      document.getElementById('meta-album-artist').value = t.album_artist || '';
      document.getElementById('meta-album').value = t.album || '';
      document.getElementById('meta-track-num').value = t.track_number || '';
      document.getElementById('meta-disc-num').value = t.disc_number || '';
      document.getElementById('meta-year').value = t.year || '';
      document.getElementById('meta-genre').value = t.genre || '';
      document.getElementById('meta-title-display').textContent = t.title;
      document.getElementById('meta-artist-display').textContent = t.artist;
      document.getElementById('meta-editor').style.display = '';
    } catch (e) { showToast('Failed to load track', 'error'); }
  },

  async save() {
    if (!this.currentTrack) return;
    try {
      await API.put(`/api/library/tracks/${this.currentTrack.id}`, {
        title: document.getElementById('meta-title').value,
        artist: document.getElementById('meta-artist').value,
        album_artist: document.getElementById('meta-album-artist').value,
        album: document.getElementById('meta-album').value,
        track_number: parseInt(document.getElementById('meta-track-num').value) || 0,
        disc_number: parseInt(document.getElementById('meta-disc-num').value) || 0,
        year: parseInt(document.getElementById('meta-year').value) || 0,
        genre: document.getElementById('meta-genre').value,
      });
      showToast('Tags saved', 'success');
      Library.loadTracks();
    } catch (e) { showToast('Save failed: ' + e.message, 'error'); }
  }
};

// --- Settings Module ---
const Settings = {
  async load() {
    try {
      const s = await API.get('/api/settings');
      document.getElementById('set-theme').value = s.theme || 'dark';
      document.getElementById('set-library-path').value = s.library_path || '';
      document.getElementById('set-download-path').value = s.download_path || '';
      document.getElementById('set-template').value = s.folder_template || '';
      document.getElementById('set-dupes').value = s.duplicate_policy || 'keep_separate';
      document.getElementById('set-max-concurrent').value = s.max_concurrent_downloads || 3;
      document.getElementById('set-max-retries').value = s.max_retries || 3;
      document.getElementById('set-auto-shutdown').checked = s.auto_shutdown !== false;
      document.getElementById('set-shutdown-timeout').value = s.shutdown_timeout || 30;
    } catch (e) { console.error(e); }
  },

  async save() {
    try {
      await API.put('/api/settings', {
        theme: document.getElementById('set-theme').value,
        library_path: document.getElementById('set-library-path').value,
        download_path: document.getElementById('set-download-path').value,
        folder_template: document.getElementById('set-template').value,
        duplicate_policy: document.getElementById('set-dupes').value,
        max_concurrent_downloads: parseInt(document.getElementById('set-max-concurrent').value) || 3,
        max_retries: parseInt(document.getElementById('set-max-retries').value) || 3,
        auto_shutdown: document.getElementById('set-auto-shutdown').checked,
        shutdown_timeout: parseInt(document.getElementById('set-shutdown-timeout').value) || 30,
      });
      showToast('Settings saved', 'success');
    } catch (e) { showToast('Save failed', 'error'); }
  }
};

// --- Logs Module ---
const Logs = {
  async refresh() {
    try {
      const data = await API.get('/api/logs');
      const el = document.getElementById('log-output');
      el.innerHTML = `<pre class="log-pre">${(data.logs || []).join('\n') || 'No logs.'}</pre>`;
      el.scrollTop = el.scrollHeight;
    } catch (e) { document.getElementById('log-output').innerHTML = '<pre class="log-pre">Failed to load logs.</pre>'; }
  },
  clear() { document.getElementById('log-output').innerHTML = '<pre class="log-pre">Logs cleared.</pre>'; }
};

// --- HTML Escape ---
function esc(s) { if (!s) return ''; const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// --- Main App ---
const App = {
  init() {
    this.setupNav();
    this.setupTheme();
    this.setupKeyboard();
    this.startHeartbeat();
    EventBus.connect();
    Library.loadTracks();
    Library.loadStats();
    Settings.load();
    Queue.updateBadge();
    Queue.startPolling();
    setInterval(() => Queue.updateBadge(), 10000);
  },

  startHeartbeat() {
    async function beat() {
      try { await API.post('/api/heartbeat', {}); } catch (e) { /* server down */ }
    }
    beat();
    setInterval(beat, 10000);
    window.addEventListener('beforeunload', () => {
      try { navigator.sendBeacon('/api/heartbeat', new Blob([], { type: 'application/json' })); } catch (e) {}
    });
  },

  setupNav() {
    document.querySelectorAll('.nav-item').forEach(item => {
      item.addEventListener('click', (e) => {
        e.preventDefault();
        const tab = item.dataset.tab;
        document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
        item.classList.add('active');
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        document.getElementById('tab-' + tab).classList.add('active');
        State.set('currentTab', tab);
        if (tab === 'queue') { Queue.load(); Queue.startPolling(); }
        else { Queue.stopPolling(); }
        if (tab === 'watch') Watch.load();
        if (tab === 'logs') Logs.refresh();
      });
    });
  },

  setupTheme() {
    const saved = localStorage.getItem('bp-theme') || 'dark';
    this.setTheme(saved);
  },

  setTheme(t) {
    document.documentElement.dataset.theme = t;
    localStorage.setItem('bp-theme', t);
    State.set('theme', t);
    const btn = document.getElementById('theme-toggle');
    btn.textContent = t === 'dark' ? '\u2600' : '\u263E';
    const sel = document.getElementById('set-theme');
    if (sel) sel.value = t;
  },

  toggleTheme() { this.setTheme(State.get('theme') === 'dark' ? 'light' : 'dark'); },

  toggleCollapse(id) {
    document.getElementById(id).classList.toggle('open');
  },

  setupKeyboard() {
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        document.getElementById('global-search').focus();
      }
    });
    document.getElementById('global-search').addEventListener('input', debounce(function() {
      if (State.get('currentTab') === 'library') {
        document.getElementById('lib-search').value = this.value;
        Library.page = 1;
        Library.loadTracks();
      }
    }, 300));
  }
};

// Init on load
document.addEventListener('DOMContentLoaded', () => App.init());
