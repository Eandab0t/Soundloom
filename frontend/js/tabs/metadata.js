/* Soundloom metadata editor tab. */
import { API, showToast, esc, debounce, attachArtistAutocomplete, coverImg } from '../core.js';

export const Metadata = {
  currentTrack: null,

  init() {
    // Album-artist suggestions come from artists already in the library.
    attachArtistAutocomplete(document.getElementById('meta-album-artist'));
  },

  searchDebounce: debounce(async function () {
    const q = document.getElementById('meta-search').value.trim();
    const el = document.getElementById('meta-results');
    if (!q) { el.style.display = 'none'; return; }
    try {
      const data = await API.get(`/api/library/tracks?search=${encodeURIComponent(q)}&limit=10`);
      if (!data.tracks.length) {
        el.innerHTML = '<p class="muted" style="padding:8px 2px">No matches</p>';
        el.style.display = '';
        return;
      }
      el.innerHTML = data.tracks.map(t => `
        <div class="artist-row" data-track-id="${t.id}">
          ${coverImg(t)}
          <span class="artist-name">${esc(t.title)}</span>
          <span class="artist-count">${esc(t.artist)}</span>
        </div>
      `).join('');
      el.querySelectorAll('.artist-row').forEach(row => {
        row.addEventListener('click', () => Metadata.editTrack(parseInt(row.dataset.trackId)));
      });
      el.style.display = '';
    } catch (e) { console.error(e); }
  }, 300),

  async editTrack(id) {
    try {
      const t = await API.get(`/api/library/tracks/${id}`);
      this.currentTrack = t;
      const cover = document.getElementById('meta-cover');
      if (cover) cover.src = `/api/library/tracks/${t.id}/cover`;
      document.getElementById('meta-title').value = t.title || '';
      document.getElementById('meta-artist').value = t.artist || '';
      document.getElementById('meta-album-artist').value = t.album_artist || '';
      document.getElementById('meta-album').value = t.album || '';
      document.getElementById('meta-track-num').value = t.track_number || '';
      document.getElementById('meta-disc-num').value = t.disc_number || '';
      document.getElementById('meta-year').value = t.year || '';
      document.getElementById('meta-genre').value = t.genre || '';
      document.getElementById('meta-title-display').textContent = t.title || '(untitled)';
      document.getElementById('meta-artist-display').textContent = t.artist || '';
      document.getElementById('meta-editor').style.display = '';
    } catch (e) { showToast('Failed to load track', 'error'); }
  },

  async save() {
    if (!this.currentTrack) return;
    try {
      await API.put(`/api/library/tracks/${this.currentTrack.id}`, {
        title: document.getElementById('meta-title').value,
        artist: document.getElementById('meta-artist').value,
        album_artist: document.getElementById('meta-album-artist').value.trim(),
        album: document.getElementById('meta-album').value,
        track_number: parseInt(document.getElementById('meta-track-num').value) || 0,
        disc_number: parseInt(document.getElementById('meta-disc-num').value) || 0,
        year: parseInt(document.getElementById('meta-year').value) || 0,
        genre: document.getElementById('meta-genre').value.trim(),
      });
      showToast('Tags saved', 'success');
      import('./library.js').then(l => l.Library.loadTracks());
    } catch (e) { showToast('Save failed: ' + e.message, 'error'); }
  },
};
