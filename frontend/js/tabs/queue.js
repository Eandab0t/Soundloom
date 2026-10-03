/* Soundloom queue tab — job list, polling, badge. */
import { API, State, showToast, esc, fmtSize } from '../core.js';
import { switchToTab } from './library.js';

const ACTIVE_STATES = ['pending', 'resolving', 'matching', 'downloading',
  'converting', 'tagging', 'organizing', 'indexing', 'backoff', 'retrying'];

export const Queue = {
  jobs: [], currentTab: 'active', pollTimer: null,

  switchTab(tab) {
    this.currentTab = tab;
    document.querySelectorAll('.seg-btn[data-tab]').forEach(b =>
      b.classList.toggle('active', b.dataset.tab === tab));
    this.load();
  },

  async load() {
    try {
      const statusMap = { active: 'active', completed: 'complete', failed: 'failed' };
      const data = await API.get(`/api/queue?status=${statusMap[this.currentTab] || ''}`);
      this.jobs = data.jobs || [];
      this.render();
    } catch (e) { console.error(e); }
  },

  render() {
    const el = document.getElementById('queue-list');
    if (!el) return;
    if (!this.jobs.length) {
      const msgs = {
        active: `
          <p>No active downloads.</p>
          <p class="muted">Queue something from the
            <a href="#add" data-goto="add">Add Music</a> or
            <a href="#sync" data-goto="sync">Sync</a> tabs, or follow an artist under
            <a href="#watch" data-goto="watch">Watch</a> — new releases land here automatically.</p>`,
        completed: '<p>No completed downloads yet.</p><p class="muted">Finished tracks appear here, and in your Library.</p>',
        failed: '<p>No failed downloads.</p>',
      };
      el.innerHTML = `<div class="empty-state">${msgs[this.currentTab]}</div>`;
      el.querySelectorAll('[data-goto]').forEach(a =>
        a.addEventListener('click', (e) => { e.preventDefault(); switchToTab(a.dataset.goto); }));
      return;
    }
    el.innerHTML = this.jobs.map(j => this._card(j)).join('');
    el.querySelectorAll('[data-action]').forEach(btn => {
      btn.addEventListener('click', () => this[btn.dataset.action](parseInt(btn.dataset.id)));
    });
  },

  _sourceTag(url) {
    // Which search source a job targets, when it has one (direct URLs don't).
    const map = { 'ytsearch1:': 'YouTube', 'scsearch1:': 'SoundCloud', 'dzsearch:': 'Deezer' };
    const hit = Object.keys(map).find(p => (url || '').startsWith(p));
    return hit ? `<span class="tag">${map[hit]}</span> ` : '';
  },

  _card(j) {
    const statusClass = ACTIVE_STATES.includes(j.status) ? 'active' : j.status;
    let statusLabel = j.status;
    if (j.status === 'backoff') {
      const nb = j.not_before ? new Date(j.not_before).toLocaleTimeString() : '';
      statusLabel = `waiting out rate limit${nb ? ` — resumes ${nb}` : ''}`;
    }
    const meta = [
      esc(j.artist || j.title || ''),
      esc((j.output_format || '').toUpperCase()),
      esc(statusLabel),
      j.error ? `<span class="err">— ${esc(j.error.substring(0, 90))}</span>` : '',
      j.output_path && j.status === 'complete' ? `<span class="ok">→ ${esc(this._basename(j.output_path))}</span>` : '',
    ].filter(Boolean).join(' · ');
    return `
      <div class="item-row">
        <div class="item-status ${statusClass}"></div>
        <div class="item-info">
          <div class="item-title">${this._sourceTag(j.source_url)}${esc(j.title || j.query || j.source_url || 'Unknown')}</div>
          <div class="item-meta">${meta}</div>
        </div>
        <div class="item-progress">
          <div class="progress"><div class="progress-fill" style="width:${j.progress || 0}%"></div></div>
        </div>
        <div class="item-actions">
          ${['failed', 'backoff'].includes(j.status)
            ? `<button class="btn ghost sm" data-action="retry" data-id="${j.id}">Retry</button>` : ''}
          <button class="btn danger sm" data-action="remove" data-id="${j.id}" title="Remove">✕</button>
        </div>
      </div>`;
  },

  _basename(p) { return (p || '').split(/[\\/]/).pop(); },

  startPolling() {
    this.stopPolling();
    this.pollTimer = setInterval(() => {
      if (State.get('currentTab') === 'queue') this.load();
    }, 2000);
  },
  stopPolling() { if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; } },

  async retry(id) { try { await API.post(`/api/queue/${id}/retry`); showToast('Requeued', 'info'); this.load(); this.updateBadge(); } catch (e) { showToast(e.message, 'error'); } },
  async remove(id) { try { await API.del(`/api/queue/${id}`); this.load(); } catch (e) { showToast(e.message, 'error'); } },
  async clearCompleted() { try { await API.post('/api/queue/clear-completed'); this.load(); } catch (e) {} },
  async clearFailed() { try { await API.post('/api/queue/clear-failed'); this.load(); } catch (e) {} },

  async updateBadge() {
    try {
      const s = await API.get('/api/queue/stats');
      const total = (s.active || 0) + (s.pending || 0);
      const badge = document.getElementById('queue-badge');
      if (badge) {
        badge.textContent = total;
        badge.style.display = total > 0 ? '' : 'none';
      }
      const summary = document.getElementById('queue-summary');
      const bits = [];
      if (s.active) bits.push(`${s.active} active`);
      if (s.pending) bits.push(`${s.pending} pending`);
      if (s.failed) bits.push(`${s.failed} failed`);
      if (summary) summary.textContent = bits.length ? bits.join(' · ') : 'Idle.';
      if (!State.get('currentTab') || State.get('currentTab') === 'queue') {
        const sb = document.getElementById('status-queue');
        if (sb) sb.textContent = bits.length ? `Queue: ${bits.join(', ')}` : 'Queue idle';
      }
    } catch (e) {}
  },
};
