/* Soundloom watch tab — artist auto-download. */
import { API, showToast, esc, attachArtistAutocomplete } from '../core.js';

export const Watch = {
  init() {
    // Autocomplete against the library; picking a name also matches Deezer
    // via watcher's normalised-name resolution.
    attachArtistAutocomplete(
      document.getElementById('watch-artist-input'),
      { onEnter: () => this.add() },
    );
  },

  async add() {
    const input = document.getElementById('watch-artist-input');
    const name = input.value.trim();
    if (!name) return;
    try {
      await API.post('/api/watch', { artist_name: name, auto_download: 1 });
      showToast(`Now watching ${name}`, 'success');
      input.value = '';
      this.load();
    } catch (e) { showToast(e.message, 'error'); }
  },

  async addFromEmpty() {
    const input = document.getElementById('watch-empty-input');
    const name = input.value.trim();
    if (!name) return;
    try {
      await API.post('/api/watch', { artist_name: name, auto_download: 1 });
      showToast(`Now watching ${name}`, 'success');
      input.value = '';
      this.load();
    } catch (e) { showToast(e.message, 'error'); }
  },

  async load() {
    const el = document.getElementById('watch-list');
    if (!el) return;
    try {
      const data = await API.get('/api/watch');
      if (!data.length) {
        el.innerHTML = `
          <div class="empty-state">
            <p>No artists being followed.</p>
            <p class="muted">Follow an artist and every new album, EP and single
            they release is queued automatically — checked about once an hour.</p>
            <div class="input-row" style="justify-content:center; margin-top:16px; gap:8px;">
              <input type="text" id="watch-empty-input" class="input sm grow" style="max-width:320px" placeholder="Artist name…" onkeydown="if(event.key==='Enter')Watch.addFromEmpty()">
              <button class="btn primary sm" onclick="Watch.addFromEmpty()">Follow artist</button>
            </div>
          </div>`;
        this.updateBadge(0);
        return;
      }
      el.innerHTML = data.map(w => this._item(w)).join('');
      el.querySelectorAll('[data-action]').forEach(btn => {
        btn.addEventListener('click', () => this[btn.dataset.action](parseInt(btn.dataset.id)));
      });
      el.querySelectorAll('input[data-role="auto"]').forEach(cb => {
        cb.addEventListener('change', () => this.toggleAuto(parseInt(cb.dataset.id), cb.checked));
      });
      this.updateBadge(data.filter(w => w.last_error).length);
    } catch (e) { console.error(e); }
  },

  _item(w) {
    const when = w.last_checked ? new Date(w.last_checked).toLocaleString() : 'never';
    const notes = [];
    if (w.last_error) notes.push(`<span class="err">${esc(w.last_error)}</span>`);
    if (w.last_enqueued) notes.push(`<span class="ok">${w.last_enqueued} queued last check</span>`);
    const note = notes.length ? ` · ${notes.join(' · ')}` : '';
    return `
      <div class="item-row">
        <div class="item-status ${w.last_error ? 'error' : 'complete'}"></div>
        <div class="item-info">
          <div class="item-title">${esc(w.artist_name)}</div>
          <div class="item-meta">checked ${esc(when)}${note}</div>
        </div>
        <label class="toggle" title="Auto-download new releases">
          <input type="checkbox" data-role="auto" data-id="${w.id}" ${w.auto_download ? 'checked' : ''}>
          <span class="toggle-slider"></span>
        </label>
        <div class="item-actions">
          <button class="btn ghost sm" data-action="checkOne" data-id="${w.id}" title="Check now and queue new releases">↻</button>
          <button class="btn danger sm" data-action="remove" data-id="${w.id}" title="Unfollow">✕</button>
        </div>
      </div>`;
  },

  updateBadge(n) {
    const b = document.getElementById('watch-badge');
    if (b) { b.textContent = n; b.style.display = n > 0 ? '' : 'none'; }
  },

  async checkAll() {
    const btn = document.getElementById('watch-check-all');
    btn.disabled = true; btn.textContent = 'Checking…';
    try {
      const r = await API.post('/api/watch/check');
      showToast(r.enqueued ? `Queued ${r.enqueued} new track(s)` : 'Checked — nothing new', 'success');
      this.load();
    } catch (e) { showToast(e.message, 'error'); }
    finally { btn.disabled = false; btn.textContent = 'Check now'; }
  },

  async checkOne(id) {
    showToast('Checking…', 'info', 1200);
    try {
      const r = await API.post(`/api/watch/${id}/check`);
      showToast(r.enqueued ? `Queued ${r.enqueued} new track(s)` : 'Nothing new', 'success');
      this.load();
    } catch (e) { showToast(e.message, 'error'); }
  },

  async toggleAuto(id, val) {
    try {
      await API.put(`/api/watch/${id}`, { auto_download: val ? 1 : 0 });
      if (val) { showToast('Auto-download on — checking now', 'info'); this.checkOne(id); }
    } catch (e) { showToast(e.message, 'error'); this.load(); }
  },

  async remove(id) {
    try { await API.del(`/api/watch/${id}`); this.load(); showToast('Unfollowed', 'info'); }
    catch (e) {}
  },
};
