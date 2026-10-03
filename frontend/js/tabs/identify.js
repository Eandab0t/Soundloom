/* Soundloom identify tab — metadata lookup review queue. */
import { API, State, showToast, esc } from '../core.js';
import { switchToTab } from './library.js';

export const Identify = {
  methods: [],

  async loadMethods() {
    const sel = document.getElementById('identify-method');
    if (!sel) return;
    try {
      const data = await API.get('/api/identify/methods');
      this.methods = data.methods;
      const usable = data.methods.filter(m => m.available);
      sel.innerHTML = usable.length
        ? usable.map(m => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join('')
        : '<option value="">none available</option>';
      const hint = document.getElementById('identify-hint');
      if (!usable.length) {
        const why = data.methods.filter(m => m.name !== 'local_only')
          .map(m => `${m.name}: ${m.reason}`).join(' · ');
        hint.innerHTML = `<span style="color:var(--warning)">No lookup source is configured. ${esc(why)}</span>`;
        document.getElementById('identify-scan-btn').disabled = true;
      } else {
        hint.textContent = `Using ${usable.length} available source${usable.length === 1 ? '' : 's'}. Suggestions below change nothing until you apply them.`;
        document.getElementById('identify-scan-btn').disabled = false;
      }
    } catch (e) { console.error(e); }
  },

  async scan() {
    const btn = document.getElementById('identify-scan-btn');
    btn.disabled = true; btn.textContent = 'Scanning…';
    try {
      const r = await API.post('/api/identify/scan', {
        limit: parseInt(document.getElementById('identify-limit').value) || 25,
        method: document.getElementById('identify-method').value,
        scope: document.getElementById('identify-scope').value,
      });
      showToast(`Scanned ${r.scanned}: ${r.matched} suggestion(s) via ${r.method}`,
        r.matched ? 'success' : 'info');
      this.load();
    } catch (e) { showToast(e.message, 'error'); }
    finally { btn.disabled = false; btn.textContent = 'Scan library'; }
  },

  async load() {
    const el = document.getElementById('identify-list');
    if (!el) return;
    try {
      const data = await API.get('/api/identify/candidates?status=pending');
      this.updateBadge(data.count);
      if (!data.candidates.length) {
        el.innerHTML = `
          <div class="empty-state">
            <p>No suggestions.</p>
            <p class="muted">Run a scan to look up your library's tags against online databases.</p>
          </div>`;
        return;
      }
      el.innerHTML = data.candidates.map(c => this._item(c)).join('');
    } catch (e) { console.error(e); }
  },

  updateBadge(n) {
    const b = document.getElementById('identify-badge');
    if (!b) return;
    b.textContent = n;
    b.style.display = n > 0 ? '' : 'none';
  },

  _item(c) {
    const pct = Math.round((c.confidence || 0) * 100);
    const cls = pct >= 85 ? 'high' : pct >= 60 ? 'mid' : '';
    const rows = Object.entries(c.diff || {}).map(([field, d]) => `
      <div class="diff-row">
        <span class="diff-field">${esc(field.replace(/_/g, ' '))}</span>
        <span class="diff-before${d.before ? '' : ' diff-empty'}">${d.before ? esc(String(d.before)) : 'empty'}</span>
        <span class="diff-arrow">→</span>
        <span class="diff-after">${esc(String(d.after))}</span>
      </div>`).join('');
    return `
      <div class="identify-item" data-id="${c.id}">
        <div class="identify-head">
          <span class="identify-title">${esc(c.current_tags?.title || '(untitled)')}</span>
          <span class="identify-conf ${cls}">${esc(c.source)} ${pct}%</span>
          <span class="muted grow" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.file_path)}</span>
          <button class="btn primary sm" data-action="apply" data-id="${c.id}">Apply</button>
          <button class="btn ghost sm" data-action="reject" data-id="${c.id}">Dismiss</button>
        </div>
        ${rows}
        ${c.notes ? `<div class="muted" style="font-size:11px;margin-top:6px">${esc(c.notes)}</div>` : ''}
      </div>`;
  },

  async apply(id) {
    try {
      const r = await API.post(`/api/identify/${id}/apply`);
      if (r.status === 'applied') {
        showToast(`Applied ${r.fields.length} field(s) to ${r.file_path.split(/[\\/]/).pop()}`, 'success', 5000);
        if (r.backup) showToast('Tag backup saved', 'info');
      } else {
        showToast('Nothing to change', 'info');
      }
      this.load();
    } catch (e) { showToast(e.message, 'error'); }
  },

  async reject(id) {
    try { await API.post(`/api/identify/${id}/reject`); this.load(); }
    catch (e) { showToast(e.message, 'error'); }
  },

  async applyAll() {
    if (!confirm('Apply every pending suggestion above the confidence threshold? A tag backup is taken first.')) return;
    const btn = document.getElementById('identify-apply-all');
    btn.disabled = true; btn.textContent = 'Applying…';
    try {
      const r = await API.post('/api/identify/apply-all', { limit: 100 });
      showToast(`Applied ${r.applied}, skipped ${r.skipped}, failed ${r.failed}`,
        r.failed ? 'error' : 'success', 6000);
      this.load();
    } catch (e) { showToast(e.message, 'error'); }
    finally { btn.disabled = false; btn.textContent = 'Apply all'; }
  },
};
