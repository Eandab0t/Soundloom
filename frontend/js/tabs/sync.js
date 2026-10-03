/* Soundloom sync tab — Soundiiz-style playlist sync UI. */
import { API, showToast, esc } from '../core.js';

export const Sync = {
  async add() {
    const input = document.getElementById('sync-url');
    const btn = document.getElementById('sync-add-btn');
    const url = input.value.trim();
    if (!url) return;
    btn.disabled = true; btn.textContent = 'Adding…';
    try {
      const r = await API.post('/api/sync', {
        url,
        quality_profile: document.getElementById('sync-quality').value,
        auto_sync: true,
        sync_now: true,
      });
      showToast(
        r.enqueued
          ? `Added — queued ${r.enqueued} new track${r.enqueued === 1 ? '' : 's'}`
          : 'Added — nothing new to download',
        'success'
      );
      input.value = '';
      this.load();
    } catch (e) {
      showToast(e.message, 'error', 6000);
    } finally {
      btn.disabled = false; btn.textContent = 'Add playlist';
    }
  },

  async load() {
    const el = document.getElementById('sync-list');
    if (!el) return;
    try {
      const data = await API.get('/api/sync');
      const lists = data.playlists || [];
      this.updateBadge(lists.filter(p => p.last_error).length);
      if (!lists.length) {
        el.innerHTML = `
          <div class="empty-state">
            <p>No synced playlists yet.</p>
            <p class="muted">Paste a Spotify or Deezer playlist URL above — Soundloom will pull
            anything from it that your library is missing, and keep it up to date if auto-sync is on.</p>
          </div>`;
        return;
      }
      el.innerHTML = lists.map(p => this._item(p)).join('');
      el.querySelectorAll('[data-action]').forEach(btn => {
        btn.addEventListener('click', () => this[btn.dataset.action](parseInt(btn.dataset.id)));
      });
      el.querySelectorAll('input[data-role="auto"]').forEach(cb => {
        cb.addEventListener('change', () => this.toggleAuto(parseInt(cb.dataset.id), cb.checked));
      });
    } catch (e) { console.error(e); }
  },

  _item(p) {
    const when = p.last_synced ? new Date(p.last_synced).toLocaleString() : 'never';
    const err = p.last_error
      ? `<span class="err">${esc(p.last_error)}</span> · ` : '';
    const meta = `${esc(p.source_type)} · last synced ${esc(when)} · ${err}${esc(p.source_ref)}`;
    return `
      <div class="item-row">
        <div class="item-status ${p.last_error ? 'error' : 'complete'}"></div>
        <div class="item-info">
          <div class="item-title">${esc(p.name)}</div>
          <div class="item-meta">${meta}</div>
        </div>
        <label class="toggle" title="Sync automatically on schedule">
          <input type="checkbox" data-role="auto" data-id="${p.id}" ${p.auto_sync ? 'checked' : ''}>
          <span class="toggle-slider"></span>
        </label>
        <div class="item-actions">
          <button class="btn ghost sm" data-action="syncOne" data-id="${p.id}">Sync now</button>
          <button class="btn danger sm" data-action="remove" data-id="${p.id}" title="Stop syncing">✕</button>
        </div>
      </div>`;
  },

  updateBadge(n) {
    const b = document.getElementById('sync-badge');
    if (b) { b.textContent = n; b.style.display = n > 0 ? '' : 'none'; }
  },

  async toggleAuto(id, val) {
    try {
      await API.put(`/api/sync/${id}`, { auto_sync: val ? 1 : 0 });
      showToast(val ? 'Auto-sync on' : 'Auto-sync off', 'info');
      if (val) this.syncOne(id, true);
    } catch (e) { showToast(e.message, 'error'); this.load(); }
  },

  async syncOne(id, quiet = false) {
    if (!quiet) showToast('Syncing…', 'info', 1500);
    try {
      const r = await API.post(`/api/sync/${id}/sync`);
      if (r.errors?.length) {
        showToast(r.errors[0], 'error', 6000);
      } else {
        showToast(
          r.enqueued
            ? `${r.name}: queued ${r.enqueued} new track${r.enqueued === 1 ? '' : 's'}`
            : `${r.name}: nothing new (${r.owned} owned, ${r.already_queued} queued)`,
          'success', 5000
        );
      }
      this.load();
    } catch (e) { showToast(e.message, 'error', 6000); this.load(); }
  },

  async syncAll() {
    showToast('Syncing all playlists…', 'info', 1500);
    try {
      const r = await API.post('/api/sync/sync-all');
      const enq = r.results.reduce((n, x) => n + (x.enqueued || 0), 0);
      const failed = r.results.filter(x => x.error).length;
      showToast(
        failed
          ? `Synced with ${failed} failure(s) — ${enq} new tracks queued`
          : enq ? `Queued ${enq} new track${enq === 1 ? '' : 's'}` : 'Everything already owned',
        failed ? 'error' : 'success', 5000
      );
      this.load();
    } catch (e) { showToast(e.message, 'error'); }
  },

  async remove(id) {
    try { await API.del(`/api/sync/${id}`); this.load(); showToast('Stopped syncing playlist', 'info'); }
    catch (e) { showToast(e.message, 'error'); }
  },
};
