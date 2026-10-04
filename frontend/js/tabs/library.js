/* Soundloom library tab — tracks / artists / albums views. */
import { API, State, showToast, esc, fmtDuration, fmtSize, debounce, coverImg } from '../core.js';
import { Player } from '../player.js';

export const Library = {
  tracks: [], total: 0, page: 1, perPage: 100, sortBy: 'title', order: 'ASC',
  subView: 'tracks', _albumFilter: '', statusFilter: '', state: null,

  async loadTracks() {
    const search = document.getElementById('lib-search')?.value || '';
    const artist = document.getElementById('lib-artist-filter')?.value || '';
    const format = document.getElementById('lib-format-filter')?.value || '';
    const album = this._albumFilter || '';
    const offset = (this.page - 1) * this.perPage;
    try {
      // '' means the active-and-playable library, which is the backend's
      // default too; 'archived' and 'all' deliberately keep rows whose file
      // is missing, because that is what a review of either list is for.
      const libraryStatus =
        this.statusFilter === 'archived' ? 'archived'
        : this.statusFilter === 'all' ? 'all'
        : 'active';
      const params = new URLSearchParams({
        sort: this.sortBy, order: this.order, search, artist, album, format,
        library_status: libraryStatus, limit: this.perPage, offset
      });
      if (this.statusFilter === 'missing') params.set('status', 'missing');
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
      <tr data-track-id="${t.id}" class="${t.file_status === 'missing' ? 'is-missing' : ''}">
        <td class="lib-play-cell">
          ${coverImg(t)}
          ${t.file_status === 'missing'
            ? `<span class="lib-gone" title="This file is not on disk"
                     aria-label="This file is not on disk">-</span>`
            : `<button class="lib-play" type="button" data-play="${t.id}"
                 title="Play ${esc(t.title)}" aria-label="Play ${esc(t.title)}">
                 <svg viewBox="0 0 24 24"><path d="M6 3l14 9-14 9z"/></svg>
               </button>`}
        </td>
        <td class="track-title">
          <span class="track-name">${esc(t.title)}</span>
          <button class="lib-queue" type="button" data-queue="${t.id}"
                  title="Add to queue (Shift-click: play next)"
                  aria-label="Add ${esc(t.title)} to queue">+</button>
        </td>
        <td>${esc(t.artist)}</td>
        <td>${esc(t.album)}</td>
        <td class="num">${fmtDuration(t.duration)}</td>
        <td><span class="tag">${esc((t.format || '').toUpperCase())}</span></td>
        <td class="num">${fmtSize(t.file_size)}</td>
        <td class="row-actions">${this.actionCell(t)}</td>
      </tr>
    `).join('');
    tbody.querySelectorAll('tr[data-track-id]').forEach(row => {
      row.addEventListener('dblclick', () => {
        import('./metadata.js').then(m => m.Metadata.editTrack(parseInt(row.dataset.trackId)));
        switchToTab('metadata');
      });
    });

    // Four ways in, matching what a library should let you do: play just this
    // track, play it and everything after it, add it to the end, or jump the
    // line. The + takes Shift-click for play-next, the same gesture the play
    // button uses for play-from-here.
    tbody.querySelectorAll('[data-play]').forEach(btn => {
      const id = Number(btn.dataset.play);
      const track = this.tracks.find(t => t.id === id);
      if (!track) return;
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        this.playFrom(id, track, e.shiftKey);
      });
    });
    tbody.querySelectorAll('[data-queue]').forEach(btn => {
      const id = Number(btn.dataset.queue);
      const track = this.tracks.find(t => t.id === id);
      if (!track) return;
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (e.shiftKey) Player.playNext([track]);
        else Player.enqueue([track]);
      });
    });

    tbody.querySelectorAll('[data-locate]').forEach(btn => {
      const track = this.tracks.find(t => t.id === Number(btn.dataset.locate));
      if (!track) return;
      btn.addEventListener('click', (e) => { e.stopPropagation(); this.locateTrack(track); });
    });
    tbody.querySelectorAll('[data-archive]').forEach(btn => {
      const track = this.tracks.find(t => t.id === Number(btn.dataset.archive));
      if (!track) return;
      btn.addEventListener('click', (e) => { e.stopPropagation(); this.archiveTrack(track); });
    });
    tbody.querySelectorAll('[data-restore]').forEach(btn => {
      const track = this.tracks.find(t => t.id === Number(btn.dataset.restore));
      if (!track) return;
      btn.addEventListener('click', (e) => { e.stopPropagation(); this.restoreTrack(track); });
    });
  },

  /**
   * What a row offers depends on its state, and nothing else.
   *
   * An available track needs no actions - it plays. A missing track gets
   * the two things that are actually true about it: find the file, or stop
   * carrying it in the active list. An archived track gets exactly one
   * button, because restore is the whole promise of archiving being a
   * database state rather than a deletion.
   */
  actionCell(t) {
    if (t.library_status === 'archived') {
      return `<button class="btn ghost xs" type="button" data-restore="${t.id}">Restore</button>`;
    }
    if (t.file_status === 'missing') {
      return `<button class="btn ghost xs" type="button" data-locate="${t.id}">Locate file</button>` +
             `<button class="btn ghost xs" type="button" data-archive="${t.id}">Archive</button>`;
    }
    return '';
  },

  /**
   * Look for a missing file by content before offering anything else.
   *
   * A SHA-256 match is proof the file is the same bytes. A title match is
   * not: different recordings share titles constantly. So the search runs
   * first and, if it finds an exact match, says so; if it finds nothing,
   * it says that too rather than quietly falling through to a name search.
   */
  async locateTrack(t) {
    showToast(`Searching the library for “${t.title}”…`, 'info');
    let found = null;
    try {
      const r = await API.post(`/api/library/tracks/${t.id}/find`, {});
      if (r.status === 'found' && r.candidates && r.candidates.length) found = r.candidates[0];
      else if (r.status === 'unknown') showToast(r.reason, 'info');
      else showToast('No file in the library has the same content.', 'info');
    } catch (e) {
      showToast('Search failed: ' + e.message, 'error');
      return;
    }

    if (found) {
      const ok = confirm(
        `Found the same audio at:\n\n${found.discovered_path}\n\n` +
        `SHA-256 ${found.sha256.slice(0, 16)}… matches.\n\n` +
        `Point this track at that file?`);
      if (ok) await this.applyLocated(t, found.discovered_path);
      return;
    }
    this.promptForPath(t);
  },

  /**
   * Manual locate. There is no native folder picker reachable from a web
   * app, so this asks for the full path and shows what it is about to do
   * before doing it. The path is validated server-side too: it has to be a
   * real file inside the configured library, and no other track may already
   * claim it.
   */
  promptForPath(t) {
    const p = prompt(
      `Full path to the file for “${t.title}”:\n\n` +
      `Currently: ${t.file_path}`,
      t.file_path || '');
    if (!p) return;
    this.applyLocated(t, p.trim());
  },

  async applyLocated(t, path) {
    try {
      const r = await API.post(`/api/library/tracks/${t.id}/locate`, { path });
      showToast(`Located “${r.track.title}”`, 'success');
      await this.loadTracks();
      await this.loadState();
    } catch (e) {
      showToast('Could not locate: ' + e.message, 'error');
    }
  },

  async archiveTrack(t) {
    const reason = prompt(
      `Archive “${t.title}”?\n\n` +
      `This only hides it from your active library. The record, its metadata, ` +
      `its original path and its history all stay, and you can restore it at any time.`,
      'file missing');
    if (reason === null) return;
    try {
      await API.post(`/api/library/tracks/${t.id}/archive`, { reason });
      showToast(`Archived “${t.title}”`, 'success');
      await this.loadTracks();
      await this.loadState();
    } catch (e) {
      showToast('Could not archive: ' + e.message, 'error');
    }
  },

  async restoreTrack(t) {
    try {
      await API.post(`/api/library/tracks/${t.id}/unarchive`, {});
      showToast(`Restored “${t.title}”`, 'success');
      await this.loadTracks();
      await this.loadState();
    } catch (e) {
      showToast('Could not restore: ' + e.message, 'error');
    }
  },

  setStatusFilter(value) {
    this.statusFilter = value || '';
    this.page = 1;
    const sel = document.getElementById('lib-status-filter');
    if (sel) sel.value = value || '';
    this.loadTracks();
    this.loadState();
  },

  /**
   * The counts line. The three buckets partition the library, so the
   * numbers always add up to the total - a missing file that is also
   * archived is counted once, as archived.
   */
  async loadState() {
    try {
      const s = await API.get('/api/library/state');
      this.state = s;
      const put = (id, v) => {
        const el = document.getElementById(id);
        if (el) el.textContent = String(v);
      };
      put('count-all', s.all_tracks);
      put('count-available', s.available);
      put('count-missing', s.missing);
      put('count-archived', s.archived);
      const active = this.statusFilter || 'present';
      document.querySelectorAll('#lib-counts .lib-count').forEach(b =>
        b.classList.toggle('active',
          b.dataset.status === (active === '' ? 'present' : active)));
      const warn = document.querySelector('#lib-counts [data-status="missing"]');
      if (warn) warn.classList.toggle('is-zero', !s.missing);
      this._wireCounts();
    } catch (e) { /* counts are informative, not required */ }
  },

  /** Counts-line buttons drive the same filter the dropdown does. */
  _wireCounts() {
    if (this._countsWired) return;
    this._countsWired = true;
    document.querySelectorAll('#lib-counts .lib-count').forEach(btn => {
      btn.addEventListener('click', () => {
        const wanted = btn.dataset.status;
        // 'all' means everything; 'present' is the default active view.
        this.setStatusFilter(wanted === 'all' ? 'all' : wanted);
      });
    });
  },

  /**
   * Play one track. Plain click plays it alone; Shift-click plays it and
   * everything after it, which is the gesture every other music player uses.
   */
  playFrom(id, track, withRest = false) {
    const i = this.tracks.findIndex(t => t.id === id);
    const list = withRest && i >= 0 ? this.tracks.slice(i) : [track];
    Player.setQueue(list, 0);
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
      this.loadState();
    } catch (e) {}
  },
};

export function switchToTab(tab) {
  const nav = document.querySelector(`.nav-item[data-tab="${tab}"]`);
  if (nav) nav.click();
}
