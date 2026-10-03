/* Soundloom add-music tab — URL preview + confirm, library search. */
import { API, showToast, esc, fmtDuration, isURL, attachArtistAutocomplete, coverImg } from '../core.js';
import { switchToTab } from './library.js';

export const AddSource = {
  plan: null,

  init() {
    // A plain-name search only hits the local library, so suggest artists from it.
    attachArtistAutocomplete(document.getElementById('add-url'));
  },

  async preview() {
    const input = document.getElementById('add-url');
    const url = input.value.trim();
    if (!url) return;

    const format = document.getElementById('add-format').value;
    const quality = document.getElementById('add-quality').value;
    const previewDiv = document.getElementById('add-preview');
    const resultsDiv = document.getElementById('add-results');
    const btn = document.getElementById('add-submit-btn');

    if (isURL(url)) {
      await this.previewUrl(url, format, quality, previewDiv, resultsDiv, btn);
    } else {
      await this.search(url, resultsDiv, previewDiv, btn);
    }
  },

  async search(query, resultsDiv, previewDiv, btn) {
    btn.disabled = true; btn.textContent = 'Searching…';
    previewDiv.style.display = 'none';
    resultsDiv.style.display = '';
    try {
      const data = await API.get(`/api/library/tracks?search=${encodeURIComponent(query)}&limit=10`);
      const tracks = data.tracks || [];
      const list = document.getElementById('add-results-list');
      if (!tracks.length) {
        list.innerHTML = `
          <div class="empty-state">
            <p>Nothing in your library matches “${esc(query)}”.</p>
            <p class="muted">To fetch it from the internet, paste a YouTube, SoundCloud or
            Deezer link instead — Soundloom downloads from URLs, not names.</p>
          </div>`;
        btn.textContent = 'Search'; btn.disabled = false;
        return;
      }
      list.innerHTML = tracks.map(t => `
        <div class="item-row">
          ${coverImg(t)}
          <div class="item-info">
            <div class="item-title">${esc(t.title || 'Unknown')}</div>
            <div class="item-meta">${esc(t.artist || '')} ${t.album ? '· ' + esc(t.album) : ''} ${t.duration ? '· ' + fmtDuration(t.duration) : ''}</div>
          </div>
          <div class="item-actions"><span class="muted">already in library</span></div>
        </div>
      `).join('');
    } catch (e) {
      showToast('Search failed: ' + e.message, 'error');
      resultsDiv.style.display = 'none';
    } finally {
      btn.textContent = 'Preview'; btn.disabled = false;
    }
  },

  async previewUrl(url, format, quality, previewDiv, resultsDiv, btn) {
    btn.disabled = true; btn.textContent = 'Resolving…';
    resultsDiv.style.display = 'none';
    previewDiv.style.display = '';
    try {
      const plan = await API.post('/api/sources/preview', { url, format, quality });
      this.plan = plan;
      const confColor = plan.match_confidence >= 85 ? 'var(--success)' : plan.match_confidence >= 70 ? 'var(--warning)' : 'var(--error)';
      document.getElementById('add-preview-card').innerHTML = `
        <div style="display:flex;gap:14px;align-items:flex-start;">
          ${plan.thumbnail ? `<img src="${esc(plan.thumbnail)}" style="width:84px;height:84px;border-radius:10px;object-fit:cover;flex-shrink:0;" referrerpolicy="no-referrer">` : '<div class="cover lg">♪</div>'}
          <div class="grow" style="min-width:0;">
            <div style="font-weight:700;font-size:15px;">${esc(plan.title)}</div>
            <div class="muted" style="font-size:13px;">${esc(plan.display_artist || plan.artist)}</div>
            <div class="muted" style="font-size:12px;margin-top:4px;">
              ${plan.duration ? fmtDuration(plan.duration) + ' · ' : ''}${esc((plan.output_format || '').toUpperCase())} · ${esc(plan.bitrate || plan.quality_profile)}
            </div>
            <div style="margin-top:6px;font-size:12px;">
              <span style="color:${confColor};font-weight:700;">Match ${plan.match_confidence}%</span>
              <span class="muted" style="margin-left:6px;">${esc(plan.match_explanation)}</span>
            </div>
            ${plan.match_warnings.length ? `<div style="color:var(--warning);font-size:11px;margin-top:4px;">${plan.match_warnings.map(w => esc(w)).join(' · ')}</div>` : ''}
            <div class="muted" style="font-size:11px;margin-top:6px;word-break:break-all;"><strong>Saves to</strong> ${esc(plan.destination_path)}</div>
          </div>
        </div>`;
      btn.textContent = 'Add to queue';
      btn.disabled = false;
      btn.onclick = () => this.confirmDownload();
    } catch (e) {
      showToast('Failed to resolve: ' + e.message, 'error', 6000);
      previewDiv.style.display = 'none';
      btn.textContent = 'Preview';
      btn.disabled = false;
      btn.onclick = () => this.preview();
    }
  },

  async confirmDownload() {
    if (!this.plan) return;
    const btn = document.getElementById('add-submit-btn');
    btn.disabled = true; btn.textContent = 'Adding…';
    try {
      await API.post('/api/sources/confirm', this.plan);
      showToast('Added to download queue', 'success');
      document.getElementById('add-url').value = '';
      document.getElementById('add-preview').style.display = 'none';
      this.plan = null;
      import('./queue.js').then(q => { q.Queue.load(); q.Queue.updateBadge(); });
    } catch (e) {
      showToast('Failed to add: ' + e.message, 'error');
    } finally {
      btn.textContent = 'Preview'; btn.disabled = false;
      btn.onclick = () => this.preview();
    }
  },

  cancelPreview() {
    this.plan = null;
    document.getElementById('add-preview').style.display = 'none';
    const btn = document.getElementById('add-submit-btn');
    btn.textContent = 'Preview'; btn.disabled = false;
    btn.onclick = () => this.preview();
  },
};
