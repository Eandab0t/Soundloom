/* Soundloom — app entry point.
   Wires navigation, theme, heartbeat and the WebSocket event bus to the
   per-tab modules. ES modules, no build step. */
import { API, State, Bus, debounce } from './core.js';
import { Library, switchToTab } from './tabs/library.js';
import { Queue } from './tabs/queue.js';
import { Sync } from './tabs/sync.js';
import { Playlists } from './tabs/playlists.js';
import { Watch } from './tabs/watch.js';
import { AddSource } from './tabs/add.js';
import { Identify } from './tabs/identify.js';
import { Metadata } from './tabs/metadata.js';
import { Convert } from './tabs/convert.js';
import { Settings } from './tabs/settings.js';
import { Logs } from './tabs/logs.js';
import { Fix } from './tabs/fix.js';

const App = {
  init() {
    this.setupNav();
    this.setupTheme();
    this.setupKeyboard();
    this.startHeartbeat();
    this.wireEvents();
    Bus.connect();

    Library.loadTracks();
    Library.loadStats();
    Settings.load();
    Watch.init();
    AddSource.init();
    Metadata.init();
    Fix.scan();          // read-only duplicate-artist check for the Library banner
    Queue.updateBadge();
    Queue.startPolling();
    setInterval(() => Queue.updateBadge(), 10000);
  },

  startHeartbeat() {
    async function beat() {
      try { await API.post('/api/heartbeat', {}); } catch { /* server down */ }
    }
    beat();
    setInterval(beat, 10000);
    window.addEventListener('beforeunload', () => {
      try { navigator.sendBeacon('/api/heartbeat', new Blob([], { type: 'application/json' })); } catch {}
    });
  },

  setupNav() {
    document.querySelectorAll('.nav-item').forEach(item => {
      item.addEventListener('click', (e) => {
        e.preventDefault();
        const tab = item.dataset.tab;
        document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
        item.classList.add('active');
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        document.getElementById('tab-' + tab)?.classList.add('active');
        State.set('currentTab', tab);

        if (tab === 'queue') { Queue.load(); Queue.startPolling(); }
        else { Queue.stopPolling(); }
        if (tab === 'library') Library.loadTracks();
        if (tab === 'sync') Sync.load();
        if (tab === 'playlists') Playlists.load();
        if (tab === 'watch') Watch.load();
        if (tab === 'identify') { Identify.loadMethods(); Identify.load(); }
        if (tab === 'convert') Convert.init();
        if (tab === 'logs') Logs.refresh();
      });
    });
  },

  wireEvents() {
    // Live job updates refresh queue + badge.
    Bus.on('job_update', (d) => {
      if (State.get('currentTab') === 'queue') Queue.load();
      Queue.updateBadge();
      if (d.status === 'complete') {
        Library.loadStats();
        if (State.get('currentTab') === 'library') Library.loadTracks();
      }
    });
    Bus.on('library_change', () => {
      Library.loadStats();
      if (State.get('currentTab') === 'library') Library.loadTracks();
    });
    Bus.on('watch_update', (d) => {
      if (State.get('currentTab') === 'watch') Watch.load();
      if (d.error) console.warn(`Watch check failed for ${d.artist}: ${d.error}`);
    });
    Bus.on('identify_update', (d) => {
      if (State.get('currentTab') === 'identify') Identify.load();
      else Identify.updateBadge(d.matched || 0);
    });
    Bus.on('sync_update', () => {
      if (State.get('currentTab') === 'sync') Sync.load();
    });
    Bus.on('log', () => {
      if (State.get('currentTab') === 'logs') Logs.refresh();
    });
  },

  setupTheme() {
    this.setTheme(localStorage.getItem('soundloom-theme') || 'dark');
  },

  setTheme(t) {
    document.documentElement.dataset.theme = t;
    localStorage.setItem('soundloom-theme', t);
    State.set('theme', t);
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = t === 'dark' ? '☀' : '☾';
    const sel = document.getElementById('set-theme');
    if (sel) sel.value = t;
  },

  toggleTheme() { this.setTheme(State.get('theme') === 'dark' ? 'light' : 'dark'); },

  toggleCollapse(id) { document.getElementById(id)?.classList.toggle('open'); },

  setupKeyboard() {
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        document.getElementById('global-search')?.focus();
      }
    });
    // Global search: mirrors into the library filter and jumps to the
    // Library tab so typing works from anywhere. (The old version read
    // `this.value` inside a debounce wrapper, which drops `this` — it
    // silently did nothing.)
    const globalSearch = document.getElementById('global-search');
    globalSearch?.addEventListener('input', debounce(() => {
      const q = globalSearch.value;
      if (State.get('currentTab') !== 'library') switchToTab('library');
      const libSearch = document.getElementById('lib-search');
      if (libSearch) libSearch.value = q;
      Library.page = 1;
      Library.loadTracks();
    }, 300));
  },
};

// Expose the modules the inline HTML onclick handlers reference.
// (AddSource and Logs were missing before — their buttons silently threw.)
Object.assign(window, { App, Library, Queue, Sync, Playlists, Watch, AddSource, Identify, Metadata, Convert, Settings, Logs, Fix });

document.addEventListener('DOMContentLoaded', () => App.init());
