/* Soundloom library tab — tracks / artists / albums views. */
import { API, State, showToast, esc, fmtDuration, fmtSize, debounce, coverImg } from '../core.js';
import { Player } from '../player.js';

export const Library = {
  tracks: [], total: 0, page: 1, perPage: 100, sortBy: 'title', order: 'ASC',
  subView: 'tracks', _albumFilter: '', statusFilter: '', state: null,
  _bfTimer: null, _moves: {},

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
    tbody.querySelectorAll('[data-restore-move]').forEach(btn => {
      const track = this.tracks.find(t => t.id === Number(btn.dataset.restoreMove));
      const move = track && this._moves[track.id];
      if (!track || !move) return;
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        this.applyLocated(track, move.discovered_path);
      });
    });
    tbody.querySelectorAll('[data-detail]').forEach(btn => {
      const track = this.tracks.find(t => t.id === Number(btn.dataset.detail));
      if (!track) return;
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        this.toggleProvenance(track, btn);
      });
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
      const move = this._moves[t.id];
      // Proof beats a prompt. With an identical digest on disk, restoring
      // is the obvious action and Locate is the fallback, not the reverse.
      // Only an *unclaimed* path offers Relink: pointing two rows at one
      // file would manufacture the duplicate instead of fixing it.
      const primary = move && move.relinkable !== false
        ? `<button class="btn xs lib-restore" type="button" data-restore-move="${t.id}"`
          + ` title="Same SHA-256 found at ${esc(move.discovered_path)}">`
          + `Restore this file</button>`
        : `<button class="btn ghost xs" type="button" data-locate="${t.id}">Locate file</button>`;
      return primary +
             `<button class="btn ghost xs" type="button" data-archive="${t.id}">Archive</button>` +
             `<button class="btn ghost xs lib-more" type="button" data-detail="${t.id}">Details</button>`;
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
      // An identical SHA-256 is proof, so this is the default action and
      // the manual path is the thing you have to ask for - not the reverse.
      const ok = confirm(
        `Found the same audio elsewhere in your library.\n\n` +
        `  ${found.discovered_path}\n\n` +
        `SHA-256 ${found.sha256.slice(0, 16)}… is identical, so this is the ` +
        `same file at a new path.\n\n` +
        `Restore this file to the track?\n` +
        `(Cancel to enter a different path yourself.)`);
      if (ok) await this.applyLocated(t, found.discovered_path);
      else this.promptForPath(t);
      return;
    }
    this.promptForPath(t);
  },

  /**
   * The provenance record for one track, shown inline.
   *
   * `never_acquired` renders as a real answer rather than an empty panel:
   * it means the acquisition table has no row for this track, which is
   * exactly what someone needs to know before concluding anything about
   * where the file went.
   */
  async toggleProvenance(t, btn) {
    const row = btn.closest('tr');
    const existing = row.querySelector('.lib-detail');
    if (existing) { existing.remove(); return; }
    try {
      const d = await API.get(`/api/library/tracks/${t.id}/provenance`);
      const tr = document.createElement('tr');
      tr.className = 'lib-detail-row';
      const cell = document.createElement('td');
      cell.colSpan = 9;
      cell.innerHTML = this.provenanceHtml(d, this._moves[t.id], t);
      tr.appendChild(cell);
      row.after(tr);
      const relink = cell.querySelector('[data-relink]');
      if (relink) {
        relink.addEventListener('click', (e) => {
          e.stopPropagation();
          this.applyLocated(t, this._moves[t.id].discovered_path);
        });
      }
    } catch (e) {
      showToast('Could not load provenance: ' + e.message, 'error');
    }
  },

  /** A digest, abbreviated head and tail: enough to compare by eye. */
  _shortSha(sha) {
    if (!sha) return '<em>none recorded</em>';
    return `<code>${esc(sha.slice(0, 4))}\u2026${esc(sha.slice(-4))}</code>`;
  },

  _provRow(label, value) {
    return `<div class="prov-row"><span class="prov-k">${esc(label)}</span>` +
           `<span class="prov-v">${value}</span></div>`;
  },

  _provHead(text, cls) {
    return `<div class="prov-head ${cls || ''}">${esc(text)}</div>`;
  },

  /**
   * What is actually known about this track, as labelled facts.
   *
   * The order is the order of how much can be done about it. A proven
   * move leads, because it is the only state with a fix attached. A track
   * with no acquisition record is labelled Unknown rather than Missing:
   * nothing has been established about it, and calling it missing claims
   * more than the evidence supports.
   */
  provenanceHtml(d, move, t) {
    const track = d.track || {};
    const acq = d.acquisitions[0];

    if (move) {
      const claim = move.claimed_by_track_id
        ? `<div class="prov-warn">That file is already in the library as ` +
          `another track${move.claimed_by_title
            ? ` (<b>${esc(move.claimed_by_title)}</b>)` : ''}, so relinking ` +
          `here would give two rows the same file. The bytes are provably ` +
          `the same track - this is a duplicate to resolve, not a missing ` +
          `file to restore.</div>`
        : '';
      const relink = move.relinkable === false ? '' :
        `<div class="prov-actions">` +
        `<button class="btn xs" type="button" data-relink="${t.id}">` +
        `Relink to this file</button></div>`;
      return this._provHead('Moved', 'prov-moved') +
        this._provRow('Expected', `<code>${esc(track.file_path || '')}</code>`) +
        this._provRow('Found', `<code>${esc(move.discovered_path || '')}</code>`) +
        this._provRow('Proof',
          `<span class="prov-exact">Exact SHA-256 match</span> ` +
          this._shortSha(move.sha256)) + claim + relink;
    }

    let head, cls = '';
    if (track.library_status === 'archived') {
      head = this._provHead('Archived', 'prov-archived');
      cls = 'prov-archived';
    } else if (d.never_acquired) {
      head = this._provHead('Unknown', 'prov-unknown');
    } else {
      head = this._provHead('Provenance', '');
    }

    let html = head;
    html += this._provRow('Last known',
      track.file_path ? `<code>${esc(track.file_path)}</code>` : '<em>unknown</em>');

    if (track.library_status === 'archived') {
      html += this._provRow('Reason',
        track.archived_reason ? esc(track.archived_reason)
                              : '<em>no reason recorded</em>');
    }

    if (acq) {
      const when = acq.completed_at
        ? esc(String(acq.completed_at).slice(0, 19).replace('T', ' '))
        : '<em>unknown</em>';
      html += this._provRow('Acquired', `${when} \u00b7 ${esc(acq.source || 'unknown')}`);
      html += this._provRow('SHA-256', this._shortSha(acq.sha256));
      if (acq.sha256 && acq.file_size) {
        html += this._provRow('Size', fmtSize(acq.file_size));
      }
      if (acq.source_url) {
        html += this._provRow('From', `<code>${esc(acq.source_url)}</code>`);
      }
      if (acq.job_id) html += this._provRow('Job', esc(String(acq.job_id)));
      if (acq.source === 'backfill') {
        html += this._provRow('Note',
          'Added by backfill \u2014 this file was already in the library, ' +
          'so this is a record of where it is, not of where it came from.');
      }
      if (!acq.sha256) {
        html += `<div class="prov-warn">Exists, but its content could not ` +
                `be read, so it cannot be identified by hash.</div>`;
      }
    } else {
      html += `<div class="prov-warn">No acquisition record: this track was ` +
              `indexed before Soundloom recorded where files came from. ` +
              `There is no digest, so this file cannot be identified by ` +
              `content \u2014 Locate will need a path.</div>`;
    }

    if (d.operations.length) {
      html += this._provRow('Journal',
        `${d.operations.length} entr${d.operations.length === 1 ? 'y' : 'ies'}`);
    }
    return html;
  },

  // --- provenance backfill --------------------------------------------

  async loadBackfill() {
    try {
      const d = await API.get('/api/library/provenance/backfill');
      this._renderBackfill(d);
      const run = d.run;
      // Poll only while something is actually running. An idle app asking
      // about progress every second is pointless when the row is durable.
      clearTimeout(this._bfTimer);
      this._bfTimer = (run && run.running)
        ? setTimeout(() => this.loadBackfill(), 700)
        : null;
    } catch (e) { /* the panel is informative, not required */ }
  },

  _renderBackfill(d) {
    const p = d.pending || {};
    const run = d.run;
    const panel = document.getElementById('bf-panel');
    if (!panel) return;
    const summary = document.getElementById('bf-summary');
    const note = document.getElementById('bf-note');
    const show = (id, on) => {
      const el = document.getElementById(id);
      if (el) el.hidden = !on;
    };

    if (!run) {
      summary.textContent = p.remaining
        ? `${p.remaining} of ${p.present} files have no recorded hash`
        : `All ${p.present} files have a recorded SHA-256`;
      show('bf-run', p.remaining > 0);
      show('bf-resume', false); show('bf-pause', false); show('bf-cancel', false);
      show('bf-progress', false);
      return;
    }

    const live = run.running;
    const resumable = !live && run.resumable;
    summary.textContent = `${run.processed_files} / ${run.total_files} files` +
      ` · ${(run.bytes_processed / 1e9).toFixed(2)} GB of ${(run.bytes_total / 1e9).toFixed(2)} GB` +
      ` · ${run.status}`;

    show('bf-run', !live && !resumable && p.remaining > 0);
    show('bf-resume', resumable);
    show('bf-pause', live);
    show('bf-cancel', live || resumable);
    show('bf-progress', true);

    const fill = document.getElementById('bf-fill');
    if (fill) fill.style.width = `${run.percent}%`;
    const stats = document.getElementById('bf-stats');
    if (stats) {
      stats.textContent = `${run.percent}% · ${run.error_count} error(s)` +
        (run.completed_at ? ` · finished ${run.completed_at.slice(11, 19)}` : '');
    }
    const cur = document.getElementById('bf-current');
    if (cur) cur.textContent = run.last_path
      ? run.last_path.split(/[\\/]/).slice(-2).join('/') : '';

    if (note) {
      let msg = '';
      if (run.status === 'interrupted') {
        msg = 'A previous run was interrupted. Resume continues from where it stopped.';
      } else if (run.status === 'paused') {
        msg = 'Paused. Finished work is kept.';
      } else if (run.status === 'complete') {
        msg = 'Complete. Every file has a recorded SHA-256, so a future ' +
              'disappearance can be proven rather than guessed.';
      } else if (run.error) {
        msg = `Failed: ${run.error}`;
      }
      note.textContent = msg;
      note.hidden = !msg;
    }
  },

  async backfillStart() {
    try {
      const r = await API.post('/api/library/provenance/backfill/start', {});
      if (r.action === 'already_running') showToast('Already running', 'info');
      else showToast('Backfill started - it runs in the background', 'success');
      this.loadBackfill();
    } catch (e) { showToast('Could not start: ' + e.message, 'error'); }
  },

  async backfillResume() {
    try {
      const r = await API.post('/api/library/provenance/backfill/resume', {});
      showToast(r.action === 'resumed' ? 'Resumed' : `Nothing to resume (${r.action})`, 'info');
      this.loadBackfill();
    } catch (e) { showToast('Could not resume: ' + e.message, 'error'); }
  },

  async backfillPause() {
    try { await API.post('/api/library/provenance/backfill/pause', {}); this.loadBackfill(); }
    catch (e) { showToast('Could not pause: ' + e.message, 'error'); }
  },

  async backfillCancel() {
    try {
      await API.post('/api/library/provenance/backfill/cancel', {});
      showToast('Stopped. Files already hashed keep their records.', 'info');
      this.loadBackfill();
    } catch (e) { showToast('Could not cancel: ' + e.message, 'error'); }
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
    // Only the Missing view can have suggestions, so only it pays for the
    // lookup. A move here is exact-hash proof, which is why it can change
    // the primary action without asking.
    if (this.statusFilter === 'missing') this.loadMoves();
  },

  /**
   * Exact-hash moved suggestions, fetched once per visit to Missing.
   *
   * Every entry is proof: only an identical SHA-256 can match. A missing
   * track with no recorded hash cannot appear, and gets the manual Locate
   * instead - which is the correct fallback, not a lesser one.
   */
  async loadMoves() {
    try {
      const d = await API.get('/api/library/moves');
      const moves = d.moves || [];
      this._moves = {};
      for (const m of moves) this._moves[m.track_id] = m;
      if (this.statusFilter === 'missing') this.renderTracks();
    } catch (e) { /* suggestions are an enhancement, never a blocker */ }
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
      this.loadBackfill();
    } catch (e) {}
  },
};

export function switchToTab(tab) {
  const nav = document.querySelector(`.nav-item[data-tab="${tab}"]`);
  if (nav) nav.click();
}
