/* Soundloom — app entry point.
   Wires navigation, theme, heartbeat and the WebSocket event bus to the
   per-tab modules. ES modules, no build step. */
import { API, State, Bus, debounce } from './core.js';
import { Library, switchToTab } from './tabs/library.js';
import { Queue } from './tabs/queue.js';
import { Attention } from './tabs/attention.js';
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
import { Updates } from './tabs/updates.js';
import { Player } from './player.js';
import { Shortcuts } from './shortcuts.js';

const App = {
  init() {
    this.setupNav();
    this.setupTheme();
    this.setupKeyboard();
    this.maybeOnboard();
    // Initialised before the tabs so a restored queue is already rendering
    // when the library's first paint lands.
    Player.init();
    Shortcuts.init();
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
    // The badge has to be right on first paint, so it is fetched at startup
    // rather than only when the tab is opened.
    Attention.updateBadge();
    setInterval(() => Attention.updateBadge(), 20000);
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
        if (tab === 'attention') { Attention.load(); Attention.updateBadge(); }
        if (tab === 'sync') Sync.load();
        if (tab === 'playlists') Playlists.load();
        if (tab === 'watch') Watch.load();
        if (tab === 'identify') { Identify.loadMethods(); Identify.load(); }
        if (tab === 'convert') Convert.init();
        if (tab === 'logs') Logs.refresh();
        if (tab === 'updates') Updates.load();
      });
    });
  },

  wireEvents() {
    // Live job updates refresh queue + badge.
    Bus.on('job_update', (d) => {
      if (State.get('currentTab') === 'queue') Queue.load();
      Queue.updateBadge();
      // A failed job may be a blocked download waiting on the user, so the
      // Needs Attention badge is re-counted on the event instead of only on
      // a timer.
      if (d.status === 'failed') Attention.updateBadge();
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

  async maybeOnboard() {
    // Skip if already dismissed, or if a library is already configured.
    if (localStorage.getItem('soundloom-onboarded') === '1') return;
    const s = await API.get('/api/settings').catch(() => ({}));
    const tracks = await API.get('/api/library/tracks?limit=1')
      .then(d => d.total || 0)
      .catch(() => 0);
    if (s.library_path || tracks > 0) return;
    Onboarding.open();
  },

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

// ============ ONBOARDING ============
// A short, dismissible first-launch flow that helps new users configure their
// music folder and discover key capabilities. Never requires accounts or cloud.
const STEPS = [
  {
    title: 'What Soundloom does',
    body: `
      <p>Soundloom is a local music-library manager that runs on your machine.
      Nothing is uploaded, and no account is required.</p>
      <ul style="margin:8px 0 0; padding-left:20px; text-align:left; max-width:420px; margin:0 auto;">
        <li>Scan and index your existing music folder</li>
        <li>Download new music from URLs (YouTube, SoundCloud, Deezer)</li>
        <li>Watch artists for new releases (auto-download)</li>
        <li>Sync Spotify/Deezer playlists into your library</li>
        <li>Fix metadata, convert formats, and play your library</li>
      </ul>
    `,
  },
  {
    title: 'Set your music folder',
    body: `
      <p>Tell Soundloom where your music lives. Open <strong>Settings &rarr; General</strong>
      and set <strong>Library path</strong> to your music folder (e.g. <code>E:/Music</code>).</p>
      <p class="muted">Soundloom only reads this folder during a scan, and only writes
      to it for tracks it acquires.</p>
    `,
  },
  {
    title: 'Scan your library',
    body: `
      <p>Back in the <strong>Library</strong> tab, click <strong>Scan</strong> to index
      every file in your music folder. This reads tags and file sizes — it does not
      rename, move, or rewrite anything.</p>
      <p class="muted">The scan runs in the background; watch progress in the Library tab.</p>
    `,
  },
  {
    title: 'You&rsquo;re all set',
    body: `
      <p>From here you can:</p>
      <ul style="margin:8px 0 0; padding-left:20px; text-align:left; max-width:420px; margin:0 auto;">
        <li><strong>Watch</strong> artists to auto-download new releases</li>
        <li><strong>Sync</strong> Spotify/Deezer playlists into your library</li>
        <li><strong>Add Music</strong> from a URL or your own files</li>
        <li><strong>Identify</strong> and fix metadata against online databases</li>
      </ul>
      <p class="muted">Keyboard: Ctrl+K searches your library; Space plays/pauses; ? shows all shortcuts.</p>
    `,
  },
];

export const Onboarding = {
  step: 0,
  open() {
    const sheet = document.getElementById('onboarding-sheet');
    if (!sheet) return;
    this.step = 0;
    sheet.hidden = false;
    this.render();
  },
  close() {
    const sheet = document.getElementById('onboarding-sheet');
    if (!sheet) return;
    sheet.hidden = true;
  },
  render() {
    const body = document.getElementById('onboarding-body');
    const btnNext = document.getElementById('onboarding-next');
    const btnSkip = document.getElementById('onboarding-skip');
    const step = STEPS[this.step];
    if (!body) return;
    body.innerHTML = `
      <div style="margin-bottom:14px">
        <span style="font-size:11px; font-weight:700; letter-spacing:0.12em; text-transform:uppercase; color:var(--text-3)">Step ${this.step + 1} of ${STEPS.length}</span>
        <h3 style="margin:6px 0 4px">${step.title}</h3>
        ${step.body}
      </div>
      <div class="muted" style="font-size:11px">Press Esc or &times; to skip at any time.</div>`;
    if (btnNext) btnNext.textContent = this.step === STEPS.length - 1 ? 'Finish' : 'Next';
    if (btnSkip) btnSkip.textContent = 'Skip';
  },
  next() {
    if (this.step < STEPS.length - 1) {
      this.step++;
      this.render();
    } else {
      this.finish();
    }
  },
  finish() {
    localStorage.setItem('soundloom-onboarded', '1');
    this.close();
  },
  wire() {
    const sheet = document.getElementById('onboarding-sheet');
    if (!sheet) return;
    sheet.querySelector('[data-close]')?.addEventListener('click', () => this.finish());
    document.getElementById('onboarding-skip')?.addEventListener('click', () => this.finish());
    document.getElementById('onboarding-next')?.addEventListener('click', () => this.next());
    sheet.addEventListener('click', (e) => {
      if (e.target === sheet) this.finish();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !sheet.hidden) { e.preventDefault(); this.finish(); }
    });
  },
};

// Wire onboarding buttons once the DOM is ready.
document.addEventListener('DOMContentLoaded', () => Onboarding.wire());
// (AddSource and Logs were missing before — their buttons silently threw.)
Object.assign(window, { App, Library, Queue, Sync, Playlists, Watch, AddSource, Identify, Metadata, Convert, Settings, Logs, Fix, Player, Attention, Shortcuts, Updates });

document.addEventListener('DOMContentLoaded', () => App.init());
