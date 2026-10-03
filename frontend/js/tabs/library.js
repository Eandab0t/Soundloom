/* Soundloom library tab — tracks / artists / albums views. */
import { API, State, showToast, esc, fmtDuration, fmtSize, debounce, coverImg } from '../core.js';

export const Library = {
  tracks: [], total: 0, page: 1, perPage: 100, sortBy: 'title', order: 'ASC',
  subView: 'tracks', _albumFilter: '',

  async loadTracks() {
    const search = document.getElementById('lib-search')?.value || '';
    const artist = document.getElementById('lib-artist-filter')?.value || '';
    const format = document.getElementById('lib-format-filter')?.value || '';
    const album = this._albumFilter || '';
    const offset = (this.page - 1) * this.perPage;
    try {
      const params = new URLSearchParams({
        sort: this.sortBy, order: this.order, search, artist, album, format,
        limit: this.perPage, offset
      });
      const data = await API.get(`/api/library/tracks?${params}`);
      this.tracks = data.tracks || [];
      this.total = data.total || 0;
      this.renderTracks();
      this.renderPagination();
      this.updateClearFilters();
    } catch (e) { console.error('Failed to load tracks:', e); }
  },

  renderTracks() {
    const tbody = document.getElementById('tracks-body');
    const empty = document.getElementById('lib-empty');
    const view = document.getElementById('lib-tracks-view');
    if (!this.tracks.length) {
      if (empty) empty.style.display = '';
      if (view) view.style.display = 'none';
      return;
    }
    if (empty) empty.style.display = 'none';
    if (view) view.style.display = '';
    tbody.innerHTML = this.tracks.map(t => `
      <tr data-track-id="${t.id}">
        <td>${coverImg(t)}</td>
        <td class="track-title">${esc(t.title)}</td>
        <td>${esc(t.artist)}</td>
        <td>${esc(t.album)}</td>
        <td class="num">${fmtDuration(t.duration)}</td>
        <td><span class="tag">${esc((t.format || '').toUpperCase())}</span></td>
        <td class="num">${fmtSize(t.file_size)}</td>
      </tr>
    `).join('');
    tbody.querySelectorAll('tr[data-track-id]').forEach(row => {
      row.addEventListener('dblclick', () => {
        import('./metadata.js').then(m => m.Metadata.editTrack(parseInt(row.dataset.trackId)));
        switchToTab('metadata');
      });
    });
  },

  renderPagination() {
    const el = document.getElementById('tracks-pagination');
    const pages = Math.ceil(this.total / this.perPage);
    if (pages <= 1) { el.innerHTML = ''; return; }
    let html = '';
    const last = Math.min(pages, 12);
    for (let i = 1; i <= last; i++) {
      html += `<button class="page-btn ${i === this.page ? 'active' : ''}" data-page="${i}">${i}</button>`;
    }
    el.innerHTML = html;
    el.querySelectorAll('.page-btn').forEach(b =>
      b.addEventListener('click', () => { this.page = parseInt(b.dataset.page); this.loadTracks(); }));
  },

  sort(col) {
    if (this.sortBy === col) { this.order = this.order === 'ASC' ? 'DESC' : 'ASC'; }
    else { this.sortBy = col; this.order = 'ASC'; }
    this.page = 1;
    document.querySelectorAll('#tracks-table th').forEach(th => th.classList.remove('sorted-asc', 'sorted-desc'));
    const th = document.querySelector(`th[data-sort="${col}"]`);
    if (th) th.classList.add(this.order === 'ASC' ? 'sorted-asc' : 'sorted-desc');
    this.loadTracks();
  },

  switchView(view) {
    this.subView = view;
    document.querySelectorAll('.seg-btn[data-view]').forEach(b =>
      b.classList.toggle('active', b.dataset.view === view));
    for (const v of ['tracks', 'artists', 'albums']) {
      const el = document.getElementById(`lib-${v}-view`);
      if (el) el.style.display = v === view ? '' : 'none';
    }
    const toolbar = document.getElementById('lib-toolbar');
    if (toolbar) toolbar.style.display = view === 'tracks' ? '' : 'none';
    if (view === 'tracks') this.loadTracks();
    else if (view === 'artists') this.loadArtists();
    else if (view === 'albums') this.loadAlbums();
  },

  async loadArtists() {
    try {
      const data = await API.get('/api/library/artists');
      const el = document.getElementById('lib-artists-view');
      const filter = document.getElementById('lib-artist-filter');
      filter.innerHTML = '<option value="">All artists</option>' +
        data.map(a => `<option value="${esc(a.name)}">${esc(a.name)} (${a.track_count})</option>`).join('');
      el.innerHTML = data.length ? data.map(a => `
        <div class="artist-row" data-artist="${esc(a.name)}">
          ${coverImg(a)}
          <span class="artist-name">${esc(a.name)}</span>
          <span class="artist-count">${a.track_count} tracks</span>
        </div>
      `).join('') : '<div class="empty-state"><p>No artists found</p></div>';
      el.querySelectorAll('.artist-row').forEach(row => {
        row.addEventListener('click', () => this.filterArtist(row.dataset.artist));
      });
    } catch (e) { console.error(e); }
  },

  async loadAlbums() {
    try {
      const data = await API.get('/api/library/albums');
      const el = document.getElementById('lib-albums-view');
      el.innerHTML = data.length ? `<div class="album-grid">${data.map(a => `
        <div class="album-card" data-album="${esc(a.name)}">
          ${coverImg(a, 'album-cover')}
          <div class="album-card-title">${esc(a.name)}</div>
          <div class="album-card-meta">${esc(a.artist || 'Unknown')} · ${a.track_count} tracks</div>
        </div>
      `).join('')}</div>` : '<div class="empty-state"><p>No albums found</p></div>';
      el.querySelectorAll('.album-card').forEach(card => {
        card.addEventListener('click', () => this.filterAlbum(card.dataset.album));
      });
    } catch (e) { console.error(e); }
  },

  filterArtist(name) {
    document.getElementById('lib-artist-filter').value = name;
    this._albumFilter = '';
    this.page = 1;
    this.switchView('tracks');
  },

  filterAlbum(name) {
    this._albumFilter = name;
    document.getElementById('lib-artist-filter').value = '';
    this.page = 1;
    this.switchView('tracks');
  },

  clearFilters() {
    this._albumFilter = '';
    this.page = 1;
    const s = document.getElementById('lib-search');
    const a = document.getElementById('lib-artist-filter');
    const f = document.getElementById('lib-format-filter');
    if (s) s.value = '';
    if (a) a.value = '';
    if (f) f.value = '';
    this.loadTracks();
  },

  updateClearFilters() {
    const btn = document.getElementById('lib-clear-filters');
    if (!btn) return;
    const has =
      (document.getElementById('lib-search')?.value || '') ||
      (document.getElementById('lib-artist-filter')?.value || '') ||
      (document.getElementById('lib-format-filter')?.value || '') ||
      this._albumFilter;
    btn.style.display = has ? '' : 'none';
  },

  debounceSearch: debounce(function () { Library.page = 1; Library.loadTracks(); }, 300),

  async scan() {
    showToast('Scanning library…', 'info');
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
      const fill = document.getElementById('scan-progress-fill');
      if (fill) fill.style.width = pct + '%';
      const txt = document.getElementById('scan-status-text');
      if (txt) txt.textContent = `Scanning: ${s.processed}/${s.total} (${s.errors} errors)`;
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
      const el = document.getElementById('status-tracks');
      if (el) el.textContent = `${s.total_tracks} tracks · ${s.total_artists} artists · ${s.total_albums} albums`;
      const summary = document.getElementById('lib-summary');
      if (summary) summary.textContent =
        `${s.total_tracks} tracks · ${s.total_artists} artists · ${s.total_albums} albums`;
    } catch (e) {}
  },
};

export function switchToTab(tab) {
  const nav = document.querySelector(`.nav-item[data-tab="${tab}"]`);
  if (nav) nav.click();
}
