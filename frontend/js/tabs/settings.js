/* Soundloom settings tab. */
import { API, showToast, esc } from '../core.js';

export const Settings = {
  sourceOrder: [],
  sourceFallback: true,

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
      val('set-spotify-secret').value = s.spotify_client_secret || '';
      val('set-soundcloud-id').value = s.soundcloud_client_id || '';
      val('set-soundcloud-secret').value = s.soundcloud_client_secret || '';
      val('set-auto-shutdown').checked = s.auto_shutdown !== false;
      val('set-shutdown-timeout').value = s.shutdown_timeout || 30;
      const redirect = document.getElementById('set-redirect-uri');
      if (redirect) {
        redirect.value = `${location.origin}/api/sync/callback`;
        redirect.title = 'Register this exact URL in your Spotify app settings';
      }
      this.loadSources();
    } catch (e) { console.error(e); }
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
      await API.put('/api/settings', {
        theme: document.getElementById('set-theme').value,
        library_path: document.getElementById('set-library-path').value,
        download_path: document.getElementById('set-download-path').value,
        folder_template: document.getElementById('set-template').value,
        duplicate_policy: document.getElementById('set-dupes').value,
        max_concurrent_downloads: parseInt(document.getElementById('set-max-concurrent').value) || 3,
        max_retries: parseInt(document.getElementById('set-max-retries').value) || 3,
        sync_interval: Math.max(3600, Math.round(hours * 3600)),
        spotify_client_id: document.getElementById('set-spotify-id').value.trim(),
        spotify_client_secret: document.getElementById('set-spotify-secret').value.trim(),
        soundcloud_client_id: document.getElementById('set-soundcloud-id').value.trim(),
        soundcloud_client_secret: document.getElementById('set-soundcloud-secret').value.trim(),
        auto_shutdown: document.getElementById('set-auto-shutdown').checked,
        shutdown_timeout: parseInt(document.getElementById('set-shutdown-timeout').value) || 30,
      });
      await this.saveSources();
      showToast('Settings saved', 'success');
    } catch (e) { showToast('Save failed: ' + e.message, 'error'); }
  },
};
