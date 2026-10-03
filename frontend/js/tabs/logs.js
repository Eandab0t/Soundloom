/* Soundloom logs tab. */
import { API } from '../core.js';

export const Logs = {
  async refresh() {
    const el = document.getElementById('log-output');
    if (!el) return;
    try {
      const data = await API.get('/api/logs');
      el.innerHTML = `<pre class="log-pre">${(data.logs || []).join('\n') || 'No logs yet.'}</pre>`;
      el.scrollTop = el.scrollHeight;
    } catch (e) {
      el.innerHTML = '<pre class="log-pre">Failed to load logs.</pre>';
    }
  },
};
