/* Soundloom settings tab. */
import { API, showToast, esc } from '../core.js';

export const Settings = {
  sourceOrder: [],
  sourceFallback: true,
  _sections: [],   // { el, label, groups: [{el, labelEl, hintEl}] }
  _searchInput: null,
  _emptyState: null,

  async load() {
    try {
      const s = await API.get('/api/settings');
      const val = (id) => document.getElementById(id);
      val('set-theme').value = s.theme || 'dark';
      val('set-library-path').value = s.library_path || '';
      val('set-download-path').value = s.download_path || '';
      val('set-template').value = s.folder_template || '';
      val('set-dupes').value = s.duplicate_policy || 'keep_separate';
      val('set-max-concurrent').value = s.max_concurrent_downloads || 3;
      val('set-max-retries').value = s.max_retries || 3;
      val('set-sync-interval-h').value = Math.round((s.sync_interval || 21600) / 3600);
      val('set-spotify-id').value = s.spotify_client_id || '';
      val('set-soundcloud-id').value = s.soundcloud_client_id || '';
      // Secrets are never returned by the API - show status, not the value,
      // and leave the field blank so saving keeps the stored secret.
      this.markSecret('set-spotify-secret', s.spotify_client_secret_configured, 'Spotify client secret');
      this.markSecret('set-soundcloud-secret', s.soundcloud_client_secret_configured, 'SoundCloud client secret');
      val('set-auto-shutdown').checked = s.auto_shutdown !== false;
      val('set-shutdown-timeout').value = s.shutdown_timeout || 30;
      const redirect = document.getElementById('set-redirect-uri');
      if (redirect) {
        redirect.value = `${location.origin}/api/sync/callback`;
        redirect.title = 'Register this exact URL in your Spotify app settings';
      }
      this.loadSources();
      this.setupPathValidation();
      this.initSettingsSearch();
    } catch (e) { console.error(e); }
  },

  initSettingsSearch() {
    const input = document.getElementById('settings-search');
    const count = document.getElementById('settings-search-count');
    if (!input) return;
    this._searchInput = input;

    // Index every collapsible section and its form groups once.
    this._sections = [];
    document.querySelectorAll('#tab-settings .collapsible').forEach(sec => {
      const labelEl = sec.querySelector('.collapsible-header > span:first-child');
      const groups = [];
      sec.querySelectorAll('.form-group, .toggle-row').forEach(g => {
        const lbl = g.querySelector('.label');
        const hint = g.querySelector('.hint');
        const labelText = lbl ? lbl.textContent : '';
        const hintText = hint ? hint.textContent : '';
        groups.push({ el: g, labelEl: lbl, hintEl: hint,
                      labelText, hintText });
      });
      this._sections.push({
        el: sec, labelEl, label: labelEl ? labelEl.textContent : '',
        groups,
      });
    });

    // Build an empty-state banner (hidden by default).
    const empty = document.createElement('div');
    empty.className = 'empty-state settings-empty';
    empty.style.display = 'none';
    empty.innerHTML = `
      <svg viewBox="0 0 24 24" class="empty-icon"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>
      <h3>No matching settings</h3>
      <p>Try a different keyword — setting names and descriptions are searched.</p>`;
    input.parentElement.appendChild(empty);
    this._emptyState = empty;

    input.addEventListener('input', (e) => this.filterSettings(e.target.value));
    this.filterSettings('');
  },

  filterSettings(query) {
    const q = (query || '').toLowerCase().trim();
    const countEl = document.getElementById('settings-search-count');
    let matchCount = 0;

    for (const sec of this._sections) {
      let sectionHasMatch = false;
      for (const g of sec.groups) {
        const haystack = (g.labelText + ' ' + (g.hintEl?.textContent || '')).toLowerCase();
        const match = q === '' || haystack.includes(q);
        if (g.labelEl) {
          // Highlight matching characters in the label.
          if (q && match) {
            g.labelEl.classList.add('setting-match');
          } else {
            g.labelEl.classList.remove('setting-match');
          }
        }
        g.el.style.display = match ? '' : 'none';
        if (match) sectionHasMatch = true;
      }
      // Show/hide the whole section based on whether any group matches.
      sec.el.style.display = sectionHasMatch || q === '' ? '' : 'none';
      if (sectionHasMatch) matchCount += sec.groups.filter(g =>
        q === '' || (g.labelText + ' ' + (g.hintEl?.textContent || '')).toLowerCase().includes(q)
      ).length;
      // Expand sections that have matches when searching.
      if (q && sectionHasMatch) sec.el.classList.add('open');
    }

    if (countEl) {
      countEl.textContent = q ? `${matchCount} match${matchCount === 1 ? '' : 'es'}` : '';
    }
    if (this._emptyState) {
      this._emptyState.style.display = (q && matchCount === 0) ? '' : 'none';
    }
    // When a search is active, collapse all non-matching sections visually
    // is handled by display:none above; expand matching ones.
    if (!q) {
      document.querySelectorAll('#tab-settings .collapsible').forEach(sec =>
        sec.classList.remove('open'));
    }
  },

  setupPathValidation() {
    for (const id of ['set-library-path', 'set-download-path']) {
      const input = document.getElementById(id);
      if (!input) continue;
      input.addEventListener('blur', () => this.validatePath(input));
      input.addEventListener('input', () => this.clearPathError(input));
    }
  },

  clearPathError(input) {
    const wrap = input.parentElement;
    if (!wrap) return;
    const old = wrap.querySelector('.path-error');
    if (old) old.remove();
    input.classList.remove('input-error');
  },

  validatePath(input) {
    const v = input.value.trim();
    if (!v) return; // empty is valid (uses default)
    this.clearPathError(input);
    // Windows absolute: drive letter or UNC. POSIX absolute: leading slash.
    const isWindowsPath = /^[a-zA-Z]:[\\/]|^\\\\/.test(v);
    const isPosixPath = /^\//.test(v);
    if (!isWindowsPath && !isPosixPath) {
      this.showPathError(input, 'Not an absolute path (e.g. C:/Music or \\\\server\\share)');
    }
  },

  showPathError(input, msg) {
    input.classList.add('input-error');
    const wrap = input.parentElement;
    if (!wrap) return;
    const err = document.createElement('span');
    err.className = 'path-error';
    err.style.cssText = 'color:var(--error); font-size:11px; margin-top:4px; display:block;';
    err.textContent = msg;
    wrap.appendChild(err);
  },

  markSecret(id, configured, label) {
    const input = document.getElementById(id);
    if (!input) return;
    input.value = '';
    input.placeholder = configured ? `Saved (${label} set) - type to replace` : `Not set`;
    input.dataset.configured = configured ? '1' : '0';
  },

  secretValue(id) {
    const input = document.getElementById(id);
    const v = input ? input.value.trim() : '';
    // Blank means "leave the stored secret alone"; the server ignores empties.
    return v || undefined;
  },

  async loadSources() {
    try {
      const data = await API.get('/api/sources/search-sources');
      this.sourceOrder = data.sources.map(s => s.key);
      this.sourceFallback = data.fallback;
      const fb = document.getElementById('set-source-fallback');
      if (fb) fb.checked = data.fallback;
      this.renderSources();
    } catch (e) { console.error(e); }
  },

  renderSources() {
    const el = document.getElementById('source-priority-list');
    if (!el) return;
    const labels = { youtube: 'YouTube', deezer: 'Deezer', soundcloud: 'SoundCloud' };
    el.innerHTML = this.sourceOrder.map((key, i) => `
      <div class="source-item" data-key="${key}">
        <span class="source-rank">${i + 1}</span>
        <span class="source-name">${esc(labels[key] || key)}</span>
        <span class="source-actions">
          <button class="icon-btn" data-move="up" data-key="${key}" title="Move up" ${i === 0 ? 'disabled' : ''}>↑</button>
          <button class="icon-btn" data-move="down" data-key="${key}" title="Move down" ${i === this.sourceOrder.length - 1 ? 'disabled' : ''}>↓</button>
        </span>
      </div>`).join('');
    el.querySelectorAll('[data-move]').forEach(btn =>
      btn.addEventListener('click', () => this.moveSource(btn.dataset.key, btn.dataset.move)));
  },

  moveSource(key, dir) {
    const i = this.sourceOrder.indexOf(key);
    const j = dir === 'up' ? i - 1 : i + 1;
    if (j < 0 || j >= this.sourceOrder.length) return;
    [this.sourceOrder[i], this.sourceOrder[j]] = [this.sourceOrder[j], this.sourceOrder[i]];
    this.renderSources();
  },

  async saveSources() {
    const fb = document.getElementById('set-source-fallback');
    try {
      const data = await API.put('/api/sources/search-sources', {
        sources: this.sourceOrder.map(key => ({ key })),
        fallback: fb ? fb.checked : true,
      });
      this.sourceOrder = data.sources.map(s => s.key);
      this.sourceFallback = data.fallback;
      this.renderSources();
    } catch (e) { showToast('Source order not saved: ' + e.message, 'error'); }
  },

  async save() {
    try {
      const hours = parseFloat(document.getElementById('set-sync-interval-h').value) || 6;
      const payload = {
        theme: document.getElementById('set-theme').value,
        library_path: document.getElementById('set-library-path').value,
        download_path: document.getElementById('set-download-path').value,
        folder_template: document.getElementById('set-template').value,
        duplicate_policy: document.getElementById('set-dupes').value,
        max_concurrent_downloads: parseInt(document.getElementById('set-max-concurrent').value) || 3,
        max_retries: parseInt(document.getElementById('set-max-retries').value) || 3,
        sync_interval: Math.max(3600, Math.round(hours * 3600)),
        spotify_client_id: document.getElementById('set-spotify-id').value.trim(),
        soundcloud_client_id: document.getElementById('set-soundcloud-id').value.trim(),
        auto_shutdown: document.getElementById('set-auto-shutdown').checked,
        shutdown_timeout: parseInt(document.getElementById('set-shutdown-timeout').value) || 30,
      };
      // Only send a secret when the user actually typed one.
      const spotifySecret = this.secretValue('set-spotify-secret');
      const soundcloudSecret = this.secretValue('set-soundcloud-secret');
      if (spotifySecret) payload.spotify_client_secret = spotifySecret;
      if (soundcloudSecret) payload.soundcloud_client_secret = soundcloudSecret;
      await API.put('/api/settings', payload);
      await this.saveSources();
      showToast('Settings saved', 'success');
      this.load();
    } catch (e) { showToast('Save failed: ' + e.message, 'error'); }
  },
};
