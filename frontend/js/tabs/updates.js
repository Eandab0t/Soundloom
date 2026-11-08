/* Update Center tab: check → select → download → review → approve → install. */
import { API } from '../core.js';

function esc(text) {
  const div = document.createElement('div');
  div.textContent = text === undefined || text === null ? '' : String(text);
  return div.innerHTML;
}

export const Updates = {
  status: null,
  selected: new Set(),
  busy: false,

  async load() { await this.refresh(); },

  async refresh() {
    try {
      this.status = await API.get('/api/updates/status');
      this.render();
      this.loadHistory();
    } catch (e) {
      const el = document.getElementById('updates-overview');
      if (el) el.innerHTML = `Failed to load update status: ${esc(e.message)}`;
    }
  },

  async check() {
    if (this.busy) return;
    this.busy = true;
    this.setButtons();
    try {
      const result = await API.post('/api/updates/check');
      if (result.error) {
        this.status.problems = [result.error];
      } else {
        this.status.last_check = result.checked_at;
        this.status.problems = result.problems || [];
        const available = new Map(result.available.map(u => [u.id, u]));
        this.status.components = this.status.components.map(c => ({
          ...c,
          update_available: available.has(c.id),
          available_version: available.get(c.id)?.version,
          available_notes: available.get(c.id)?.release_notes,
          restart_required: available.get(c.id)?.restart_required ?? c.restart_required,
        }));
      }
      this.render();
    } catch (e) {
      alert(`Update check failed: ${e.message}`);
    } finally {
      this.busy = false;
      this.setButtons();
    }
  },

  async downloadSelected() {
    const ids = [...this.selected];
    if (!ids.length || this.busy) return;
    this.busy = true;
    this.setButtons();
    try {
      const result = await API.post('/api/updates/download', { component_ids: ids });
      await this.refresh();
      const failures = Object.entries(result.results || {}).filter(([, r]) => r !== 'staged');
      if (failures.length) alert(failures.map(([id, r]) => `${id}: ${r}`).join('\n'));
    } catch (e) {
      alert(`Download failed: ${e.message}`);
      this.busy = false;
      this.setButtons();
    }
  },

  async cancelDownload() {
    try { await API.del('/api/updates/download'); } catch {}
    this.busy = false;
    this.setButtons();
    await this.refresh();
  },

  selectAll() {
    for (const c of this.status?.components || []) {
      if (c.update_available && c.type !== 'core') this.selected.add(c.id);
    }
    this.render();
  },

  clearSelection() {
    this.selected.clear();
    this.render();
  },

  toggle(id, checked) {
    if (checked) this.selected.add(id); else this.selected.delete(id);
    this.setButtons();
  },

  showReview() {
    const stagedSelected = [...this.selected].filter(id => this.status.staged.includes(id));
    if (!stagedSelected.length) return;
    const el = document.getElementById('updates-review');
    const rows = stagedSelected.map(id => {
      const c = this.status.components.find(x => x.id === id);
      return `
        <div style="border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-bottom:8px;">
          <strong>${esc(c.name)}</strong> <span class="badge">${esc(id)}</span><br/>
          Installed: ${esc(c.version)} → Available: <strong>${esc(c.available_version)}</strong><br/>
          <span style="color:var(--text-2);">${esc(c.available_notes || 'No release notes provided.')}</span><br/>
          Restart required: ${c.restart_required ? 'Yes' : 'No'}
        </div>`;
    }).join('');
    el.style.display = '';
    el.innerHTML = `
      <strong>Review updates before installing</strong>
      <p style="color:var(--text-2); font-size:13px;">
        These packages were downloaded and hash-verified. Nothing is installed until you approve.
      </p>
      ${rows}
      <div class="input-row" style="justify-content:flex-end; margin-top:10px;">
        <button class="btn ghost sm" onclick="Updates.hideReview()">Cancel</button>
        <button class="btn primary sm" onclick="Updates.installStaged()">Approve and Install</button>
      </div>`;
  },

  hideReview() {
    document.getElementById('updates-review').style.display = 'none';
  },

  async installStaged() {
    const stagedSelected = [...this.selected].filter(id => this.status.staged.includes(id));
    if (!stagedSelected.length) return;
    this.busy = true;
    this.setButtons();
    try {
      const result = await API.post('/api/updates/install', { component_ids: stagedSelected });
      this.hideReview();
      await this.refresh();
      const lines = Object.entries(result.results || {}).map(([id, r]) => `${id}: ${r}`);
      const needsRestart = stagedSelected.filter(id => {
        const c = this.status.components.find(x => x.id === id);
        return c && c.restart_required && result.results?.[id] === 'installed';
      });
      let message = lines.join('\n');
      if (needsRestart.length) message += `\n\nRestart Soundloom to finish updating: ${needsRestart.join(', ')}`;
      if (needsRestart.includes('frontend')) message += '\nThe new UI loads on the next page reload.';
      alert(message || 'Nothing installed.');
    } catch (e) {
      alert(`Install failed: ${e.message}`);
    } finally {
      this.busy = false;
      this.setButtons();
    }
  },

  async restore(id) {
    if (!confirm(`Restore ${id} to its previous version?`)) return;
    try {
      const result = await API.post('/api/updates/restore', { component_id: id });
      if (result.error) alert(result.error);
    } catch (e) {
      alert(`Restore failed: ${e.message}`);
    }
    await this.refresh();
  },

  async loadHistory() {
    const el = document.getElementById('updates-history');
    if (!el) return;
    try {
      const { history } = await API.get('/api/updates/history');
      if (!history.length) { el.innerHTML = '<div style="color:var(--text-2); font-size:13px;">No updates installed yet.</div>'; return; }
      el.innerHTML = `<div class="table-wrap"><table class="table">
        <thead><tr><th>When</th><th>Component</th><th>From</th><th>To</th><th>Result</th></tr></thead>
        <tbody>${history.slice().reverse().map(h => `
          <tr><td>${esc(h.at)}</td><td>${esc(h.component_id)}</td><td>${esc(h.from_version)}</td>
          <td>${esc(h.to_version)}</td><td>${esc(h.result)}</td></tr>`).join('')}
        </tbody></table></div>`;
    } catch (e) {
      el.innerHTML = `<div style="color:var(--text-2); font-size:13px;">History unavailable.</div>`;
    }
  },

  setButtons() {
    const downloadBtn = document.getElementById('updates-download-btn');
    const reviewBtn = document.getElementById('updates-review-btn');
    const cancelBtn = document.getElementById('updates-cancel-btn');
    if (!downloadBtn || !this.status) return;
    const stagedSelected = [...this.selected].filter(id => this.status.staged.includes(id));
    downloadBtn.disabled = this.busy || !this.selected.size;
    reviewBtn.disabled = this.busy || !stagedSelected.length;
    cancelBtn.style.display = this.busy ? '' : 'none';
  },

  render() {
    const s = this.status;
    if (!s) return;
    const overview = document.getElementById('updates-overview');
    if (overview) {
      const lastCheck = s.last_check ? new Date(s.last_check * 1000).toLocaleString() : 'never';
      const updatesCount = s.components.filter(c => c.update_available).length;
      overview.innerHTML = `
        ${s.pending_core_restart ? '<div style="color:var(--warning, #e6a23c); font-size:13px; margin-bottom:6px;"><strong>Core update staged — restart Soundloom to apply it.</strong> The update is verified and will roll back automatically if it fails to start.</div>' : ''}
        <div class="input-row" style="gap:24px; flex-wrap:wrap; font-size:13px;">
          <span>Version: <strong>${esc(s.core_version)}</strong></span>
          <span>Last check: <strong>${esc(lastCheck)}</strong></span>
          <span>Source: ${s.configured ? '<strong>configured</strong>' : '<strong>not configured</strong> — set update_manifest_url in settings'}</span>
          <span>Updates available: <strong>${updatesCount}</strong></span>
          <span>Auto mode: <strong>${esc(s.auto_update)}</strong> (downloads and installs always ask first)</span>
        </div>
        ${s.problems.length ? `<div style="color:var(--text-2); font-size:12px; margin-top:6px;">${s.problems.map(esc).join('<br>')}</div>` : ''}`;
    }
    const list = document.getElementById('updates-components');
    if (list) {
      if (!s.components.length) { list.innerHTML = '<div style="color:var(--text-2); font-size:13px;">No components found.</div>'; }
      else {
        list.innerHTML = `<div class="table-wrap"><table class="table">
          <thead><tr><th></th><th>Component</th><th>Type</th><th>Installed</th><th>Available</th><th>Status</th><th>Restart</th></tr></thead>
          <tbody>${s.components.map(c => {
            const selectable = c.type !== 'core';
            const staged = s.staged.includes(c.id);
            const rollback = s.rollbacks[c.id];
            return `<tr>
              <td class="th-select-cell">${selectable ? `<input type="checkbox" ${this.selected.has(c.id) ? 'checked' : ''} onchange="Updates.toggle('${esc(c.id)}', this.checked)">` : ''}</td>
              <td>${esc(c.name)}${rollback ? ` <button class="btn ghost sm" style="margin-left:6px;" onclick="Updates.restore('${esc(c.id)}')">Restore ${esc(rollback)}</button>` : ''}</td>
              <td>${esc(c.type)}</td>
              <td>${esc(c.version)}</td>
              <td>${esc(c.available_version || '')}</td>
              <td>${c.update_available ? '<span class="badge">update available</span>' : ''}${staged ? '<span class="badge">staged</span>' : ''}</td>
              <td>${c.update_available && c.restart_required ? 'Yes' : '—'}</td>
            </tr>`;
          }).join('')}
          </tbody></table></div>`;
      }
    }
    this.setButtons();
  },
};
