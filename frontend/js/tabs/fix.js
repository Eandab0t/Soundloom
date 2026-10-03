/* Soundloom fix tools - bulk duplicate-artist repair with preview + backup.
   Scan is read-only and runs at startup; the write only happens when the
   user reviews the grid and clicks Fix, and the backend backs up first. */
import { API, showToast, esc } from '../core.js';

export const Fix = {
  groups: [],
  selected: new Set(),

  async scan() {
    try {
      const data = await API.get('/api/autofix/duplicate-artists');
      this.groups = data.groups || [];
      this.selected = new Set(this.groups.map((_, i) => i));
      this.renderBanner(data);
    } catch { /* endpoint unavailable - stay quiet */ }
  },

  renderBanner(data) {
    const el = document.getElementById('fix-banner');
    if (!el) return;
    if (!data.tracks_affected) {
      el.style.display = 'none';
      el.innerHTML = '';
      return;
    }
    el.style.display = '';
    const example = this.groups[0]?.current_value || '';
    el.innerHTML = `
      <span class="fix-banner-icon">⚠</span>
      <span class="fix-banner-text">
        <strong>${data.tracks_affected} track${data.tracks_affected === 1 ? '' : 's'}</strong>
        have duplicated artist names (like “${esc(example)}”).
      </span>
      <button class="btn sm" id="fix-open-btn">Review &amp; fix</button>`;
    el.querySelector('#fix-open-btn').addEventListener('click', () => this.openPanel());
  },

  openPanel() {
    const el = document.getElementById('fix-panel');
    if (!el) return;
    el.style.display = '';
    el.innerHTML = `
      <div class="fix-panel">
        <div class="fix-panel-head">
          <h2>Fix duplicated artist names</h2>
          <button class="icon-btn" id="fix-close-btn" title="Close">✕</button>
        </div>
        <p class="fix-summary">
          Each row is one exact change. Untick anything you want to keep as is.
          A tag backup is taken automatically before anything is written.
        </p>
        <div class="fix-grid">
          <span class="fix-head">Now</span><span></span>
          <span class="fix-head">After fix</span><span class="fix-head">Tracks</span>
          ${this.groups.map((g, i) => `
            <span class="fix-before">${esc(g.current_value)}</span>
            <span class="fix-arrow">→</span>
            <span class="fix-after">${esc(g.fixed_value)}</span>
            <span class="fix-count">
              <input type="checkbox" class="fix-check" data-idx="${i}" checked>
              ${g.track_count}
            </span>`).join('')}
        </div>
        <div class="input-row" style="margin-top:14px">
          <button class="btn primary" id="fix-apply-btn">Fix selected</button>
          <button class="btn ghost" id="fix-cancel-btn">Not now</button>
        </div>
        <div id="fix-results"></div>
      </div>`;

    el.querySelector('#fix-close-btn').addEventListener('click', () => { el.style.display = 'none'; });
    el.querySelector('#fix-cancel-btn').addEventListener('click', () => { el.style.display = 'none'; });
    el.querySelectorAll('.fix-check').forEach(cb =>
      cb.addEventListener('change', () => {
        cb.checked ? this.selected.add(+cb.dataset.idx) : this.selected.delete(+cb.dataset.idx);
      }));
    el.querySelector('#fix-apply-btn').addEventListener('click', () => this.apply());
  },

  async apply() {
    const btn = document.getElementById('fix-apply-btn');
    const chosen = [...this.selected].map(i => this.groups[i]).filter(Boolean);
    if (!chosen.length) { showToast('Nothing selected', 'info'); return; }
    btn.disabled = true; btn.textContent = 'Fixing…';
    try {
      const r = await API.post('/api/autofix/fix-duplicate-artists', { groups: chosen });
      const results = document.getElementById('fix-results');
      if (results) {
        const rows = (r.changes || []).map(c => {
          const parts = [];
          if (c.artist) parts.push(
            `<span class="before">${esc(c.artist[0])}</span> → <span class="after">${esc(c.artist[1])}</span>`);
          if (c.album_artist) parts.push(
            `<span class="before">${esc(c.album_artist[0])}</span> → <span class="after">${esc(c.album_artist[1])}</span>`);
          return `<div class="fix-result">${parts.join(' · ') || 'no change needed'}</div>`;
        });
        const extra = rows.length > 12 ? `<p class="fix-more">…and ${rows.length - 12} more tracks</p>` : '';
        results.innerHTML = `
          <p class="fix-summary" style="margin-top:14px">
            ✅ Fixed ${r.tracks_fixed} track${r.tracks_fixed === 1 ? '' : 's'}.
            ${r.errors?.length ? `${r.errors.length} problem(s) — see logs.` : 'No errors.'}
            ${r.backup_path ? `Backup: <span class="muted">${esc(r.backup_path)}</span>` : ''}
          </p>
          ${rows.slice(0, 12).join('')}${extra}`;
      }
      showToast(`Fixed ${r.tracks_fixed} track${r.tracks_fixed === 1 ? '' : 's'} — backup saved`, 'success');
      import('./library.js').then(l => { l.Library.loadTracks(); l.Library.loadStats(); });
      this.scan();  // refresh banner (multi-variant leftovers re-surface here)
    } catch (e) {
      showToast('Fix failed: ' + e.message, 'error', 6000);
    } finally {
      btn.disabled = false; btn.textContent = 'Fix selected';
    }
  },
};
