/* Soundloom persistent player.

   Three things this has to get right, in order of how much they would annoy
   the user if it broke:

   1. **It survives navigation.** The bar is a sibling of <main>, not inside any
      .tab-panel, so switching tabs cannot tear it down. Nothing here listens
      for navigation, so nothing here has to survive it.

   2. **The queue is durable.** It lives in localStorage, so closing the app
      and coming back finds the queue, the current track, the position in it,
      and the playback settings exactly where they were. Only track ids and
      display fields are stored - the library stays the source of truth, and
      each track is re-validated from the server before it plays.

   3. **The shortcuts never steal keystrokes.** Every binding is ignored while
      the user is typing in a field, so Space types a space and S types an s.

   Shuffle keeps a separate playback order rather than scrambling the visible
   queue, so turning it off mid-album restores the order the user built. */
import { API, showToast, esc, fmtDuration, coverImg, debounce } from './core.js';

const STORAGE_KEY = 'soundloom-player';
const SCHEMA = 1;
const MAX_QUEUE = 500;
const SEEK_STEP = 5;
const VOLUME_STEP = 0.05;
const MAX_CONSECUTIVE_ERRORS = 3;

const PLAY_PATH = 'M6 3l14 9-14 9z';
const PAUSE_PATH = 'M7 4h4v16H7zM13 4h4v16h-4z';

// Only these fields are persisted. A track row carries paths and sizes that
// would bloat localStorage and go stale immediately.
const KEEP = ['id', 'title', 'artist', 'album', 'duration', 'format'];

function snapshot(track) {
  const out = {};
  for (const k of KEEP) out[k] = track[k] ?? (k === 'duration' ? 0 : '');
  return out;
}

function shuffled(items) {
  const a = [...items];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

export const Player = {
  /* Set by the shortcuts help sheet. These bindings live on `document`, and
     the sheet has no text field for the typing guard below to catch, so without
     an explicit suspension Space would pause the track behind the dialog and Q
     would toggle the queue underneath it. */
  suspended: false,
  queue: [],
  index: -1,
  shuffle: false,
  repeat: 'off',
  volume: 0.9,
  muted: false,
  queueOpen: false,

  _order: [],        // playback order: indices into queue
  _pos: -1,          // cursor into _order
  _scrubbing: false, // true while the user drags the seek bar
  _errors: 0,
  _audio: null,
  _el: {},

  // ---------------------------------------------------------------------
  // Lifecycle
  // ---------------------------------------------------------------------

  init() {
    const audio = document.getElementById('pb-audio');
    if (!audio) return;
    this._audio = audio;

    this._el = {
      root: document.getElementById('player'),
      art: document.getElementById('pb-art'),
      title: document.getElementById('pb-title'),
      artist: document.getElementById('pb-artist'),
      play: document.getElementById('pb-play'),
      playIcon: document.getElementById('pb-play-icon'),
      prev: document.getElementById('pb-prev'),
      next: document.getElementById('pb-next'),
      shuffle: document.getElementById('pb-shuffle'),
      repeat: document.getElementById('pb-repeat'),
      repeatBadge: document.getElementById('pb-repeat-badge'),
      mute: document.getElementById('pb-mute'),
      volumeIcon: document.getElementById('pb-volume-icon'),
      progress: document.getElementById('pb-progress'),
      volume: document.getElementById('pb-volume'),
      elapsed: document.getElementById('pb-elapsed'),
      duration: document.getElementById('pb-duration'),
      queue: document.getElementById('player-queue'),
      queueToggle: document.getElementById('pb-queue-toggle'),
      queueList: document.getElementById('pq-list'),
      queueCount: document.getElementById('pq-count'),
      clear: document.getElementById('pq-clear'),
      close: document.getElementById('pq-close'),
    };

    this.restore();
    this.wire();
    this.rebuildOrder();
    // restore() only sets state; apply it to the DOM now that the elements
    // exist. Without this the queue panel silently comes back closed even
    // though it was open when the app was closed.
    this.toggleQueue(this.queueOpen);
    this.render();
    this.setupKeys();
  },

  current() {
    return this.index >= 0 ? this.queue[this.index] : null;
  },

  // ---------------------------------------------------------------------
  // Persistence
  // ---------------------------------------------------------------------

  restore() {
    let saved;
    try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null'); } catch { return; }
    if (!saved || saved.schema !== SCHEMA) return;

    this.shuffle = !!saved.shuffle;
    this.repeat = ['off', 'all', 'one'].includes(saved.repeat) ? saved.repeat : 'off';
    this.volume = typeof saved.volume === 'number'
      ? Math.min(1, Math.max(0, saved.volume)) : 0.9;
    this.muted = !!saved.muted;
    this.queueOpen = !!saved.queueOpen;
    this.queue = Array.isArray(saved.queue) ? saved.queue.filter(t => t && t.id) : [];

    const idx = Number.isInteger(saved.index) ? saved.index : -1;
    this.index = idx >= 0 && idx < this.queue.length ? idx : -1;
    this._resumeAt = Number(saved.position) || 0;
  },

  save() {
    // Only trust the element's clock once something is actually loaded.
    // Before that currentTime is 0, and saving it would throw away the
    // position restore() just recovered - the resume point would be lost on
    // any save between page load and the first play.
    const loaded = !!(this._audio && this._audio.getAttribute('src'));
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        schema: SCHEMA,
        queue: this.queue,
        index: this.index,
        position: loaded ? this._audio.currentTime : (this._resumeAt || 0),
        shuffle: this.shuffle,
        repeat: this.repeat,
        volume: this.volume,
        muted: this.muted,
        queueOpen: this.queueOpen,
      }));
    } catch (err) {
      // A full or blocked localStorage must not break playback; it only
      // means the queue stops being durable.
      console.warn('Could not save the player queue:', err);
    }
  },

  // ---------------------------------------------------------------------
  // Queue manipulation
  // ---------------------------------------------------------------------

  /** Replace the queue and start at `startIndex`. */
  setQueue(tracks, startIndex = 0) {
    const list = tracks.map(snapshot);
    if (!list.length) return;
    this.queue = list.slice(0, MAX_QUEUE);
    this.index = Math.max(0, Math.min(startIndex, this.queue.length - 1));
    this.rebuildOrder();
    this._errors = 0;
    this.play();
    this.save();
  },

  /** Add to the end of the queue without interrupting what is playing. */
  enqueue(tracks) {
    const list = tracks.map(snapshot).filter(t => t.id);
    if (!list.length) return;
    const room = MAX_QUEUE - this.queue.length;
    if (room <= 0) { showToast('Queue is full', 'warning'); return; }
    const added = list.slice(0, room);
    const at = this.queue.length;
    const newIdx = added.map((_, i) => at + i);
    this.queue.push(...added);
    const wasEmpty = this.index < 0;
    if (wasEmpty) {
      // Nothing was playing, so these become the queue. The index has to be
      // set *before* the rebuild: rebuildOrder puts the current track at the
      // front and records where it landed in _pos, so rebuilding first left
      // _pos at -1 while index was already 0, and "next" then replayed the
      // track that had just started.
      this.index = 0;
      this.rebuildOrder();
      this.play();
    } else {
      this._appendToOrder(newIdx);
    }
    this.save();
    this.render();
    showToast(`Added ${added.length} to the queue`);
  },

  /** Insert directly after the current track, so it plays next. */
  playNext(tracks) {
    const list = tracks.map(snapshot).filter(t => t.id);
    if (!list.length) return;
    if (!this.queue.length) return this.setQueue(list, 0);

    const room = MAX_QUEUE - this.queue.length;
    if (room <= 0) { showToast('Queue is full', 'warning'); return; }
    const added = list.slice(0, room);
    // Append, then move the new indices to the front of what is left, which
    // is exactly "plays next" without disturbing the order the user built.
    const at = this.queue.length;
    this.queue.push(...added);
    const newIdx = added.map((_, i) => at + i);
    this.rebuildOrder();
    this._reinsertAfterCurrent(newIdx);

    this.save();
    this.render();
    showToast(added.length === 1
      ? `Playing next: ${added[0].title}`
      : `Playing next: ${added.length} tracks`);
  },

  removeAt(i) {
    if (i < 0 || i >= this.queue.length) return;
    const wasCurrent = i === this.index;
    const wasPos = this._pos;
    const at = this._order.indexOf(i);      // where it sat in playback order
    this.queue.splice(i, 1);
    if (!this.queue.length) {
      this.stop();
    } else {
      // Renumber the playback order around the hole instead of rebuilding it.
      // Everything after the removed slot shifts down by one, and the removed
      // slot itself drops out; a rebuild would be simpler but would silently
      // undo a "play next" the user had just set up.
      this._order = this._order
        .map(x => (x === i ? -1 : x > i ? x - 1 : x))
        .filter(x => x >= 0);
      if (at >= 0 && at < wasPos) this._pos = wasPos - 1;
      if (i < this.index) this.index -= 1;
      if (wasCurrent) {
        // The removed track's slot in the order is now held by whatever was
        // going to play next, so take playback from there.
        if (this._order.length) {
          const pos = Math.min(at < 0 ? wasPos : at, this._order.length - 1);
          this._pos = pos;
          this.index = this._order[pos];
        } else {
          this.index = Math.min(i, this.queue.length - 1);
          this.rebuildOrder();
        }
        this._errors = 0;
        this.play();
      }
    }
    this.save();
    this.render();
  },

  clearQueue() {
    this.stop();
    this.queue = [];
    this.index = -1;
    this.rebuildOrder();
    this.save();
    this.render();
  },

  /**
   * Rebuild the playback order.
   *
   * With shuffle off the playback order *is* the queue order, and the cursor
   * sits on the queue index, so "next" means the next track the user can see.
   * Rebuilding it as [current, ...rest] instead rewind on every jump into the
   * middle: starting on the third of five used to play 3, 1, 2, 4, 5.
   *
   * With shuffle on the visible queue stays exactly as the user left it and
   * this is a separate permutation, with the current track pinned to the front
   * so that "next" is simply `_order[_pos + 1]`.
   */
  rebuildOrder() {
    const all = this.queue.map((_, i) => i);
    const cur = this.index;
    if (cur < 0) {
      this._order = this.shuffle ? shuffled(all) : all;
      this._pos = -1;
      return;
    }
    if (this.shuffle) {
      this._order = [cur, ...shuffled(all.filter(i => i !== cur))];
      this._pos = 0;
    } else {
      this._order = all;
      this._pos = cur;
    }
  },

  /** Move queue indices to sit immediately after the current track. */
  _reinsertAfterCurrent(indices) {
    const set = new Set(indices);
    const rest = this._order.filter(i => !set.has(i) && i !== this.index);
    const moved = indices.filter(i => i !== this.index);
    // With shuffle off the queue is the sequence, so only what sits after the
    // cursor is still ahead of us - dragging the already-passed tracks back in
    // here would rewind a second time, right after the jump we just honoured.
    // With shuffle on every waiting track is still in play, so the tail is
    // drawn from all of them.
    const pool = (this.shuffle || this.index < 0)
      ? rest
      : rest.filter(i => i > this.index);
    const tail = this.shuffle ? shuffled(pool) : pool;
    this._order = this.index >= 0 ? [this.index, ...moved, ...tail] : tail;
    this._pos = this.index >= 0 ? 0 : -1;
  },

  /**
   * Put queue indices at the end of the playback order, leaving the rest of
   * it exactly as it was. "Add to queue" is a promise that the queue keeps
   * playing in the order it already has.
   */
  _appendToOrder(indices) {
    if (!indices.length) return;
    if (!this._order.length) { this.rebuildOrder(); return; }
    this._order = [...this._order, ...indices];
  },

  // ---------------------------------------------------------------------
  // Playback
  // ---------------------------------------------------------------------

  async play() {
    const track = this.current();
    if (!track) return;

    const resume = this._resumeAt || 0;
    this._resumeAt = 0;
    this._errors = 0;

    // Re-validate against the library rather than trusting the stored row: a
    // track can have been renamed, re-tagged or deleted while the app was
    // closed, and a deleted file must fail loudly here rather than silently
    // sitting at 0:00 forever.
    let fresh = track;
    try {
      const row = await API.get(`/api/library/tracks/${track.id}`);
      if (row && row.id) {
        fresh = snapshot(row);
        this.queue[this.index] = fresh;
      }
    } catch (err) {
      showToast(`"${track.title}" is no longer in your library`, 'warning');
      this._skip(1);
      return;
    }

    this._audio.src = `/api/library/tracks/${fresh.id}/audio`;
    this._audio.volume = this.muted ? 0 : this.volume;
    this.render();
    this._updateMediaSession(fresh);

    try {
      await this._audio.play();
    } catch (err) {
      // Autoplay policies and a missing codec both land here.
      if (err && err.name === 'NotAllowedError') {
        this.render();
        return;                       // paused until the user hits play
      }
      this._onError();
      return;
    }

    // Apply the resume point. `loadedmetadata` only fires when the duration is
    // not already known, so set it directly too and let whichever applies win
    // - both paths clear `pending`, so a seek can only happen once.
    if (resume > 0) this._seekOnce(resume);
  },

  _seekOnce(seconds) {
    const a = this._audio;
    if (!a) return;
    let pending = seconds;
    const apply = () => {
      if (pending <= 0) return;
      const dur = a.duration;
      const target = dur && Number.isFinite(dur) ? Math.min(pending, dur - 0.25) : pending;
      pending = 0;
      a.currentTime = Math.max(0, target);
      this.render();
    };
    if (a.readyState >= 1) { apply(); return; }
    a.addEventListener('loadedmetadata', apply, { once: true });
  },

  toggle() {
    if (!this.current()) return;
    if (this._audio.paused) this.play();
    else this._audio.pause();
    this.render();
  },

  pause() {
    this._audio.pause();
    this.render();
  },

  stop() {
    this._audio.pause();
    this._audio.removeAttribute('src');
    this._audio.load();
    this._resumeAt = 0;
    this.index = -1;
    this.rebuildOrder();
    this.render();
  },

  next(userInitiated = true) {
    if (!this.queue.length) return;
    this._resumeAt = 0;

    if (this._pos + 1 < this._order.length) {
      this._goto(this._pos + 1);
      return;
    }

    if (this.repeat === 'all') {
      // Wrap to the top of the queue, not back to the track that just ended -
      // replaying the last track would make repeat-all loop on it forever.
      // The order is rebuilt from scratch, so shuffle picks a fresh sequence
      // while the queue itself (and therefore the visible order) is untouched.
      this._errors = 0;
      const all = this.queue.map((_, i) => i);
      this._order = this.shuffle ? shuffled(all) : all;
      this._pos = -1;
      this._goto(0);
      return;
    }

    // End of the queue. Stop where we are rather than silently wrapping -
    // wrapping when the user did not ask for repeat is the classic surprise.
    if (userInitiated) showToast('End of queue');
    this._audio.pause();
    this.render();
  },

  prev() {
    if (!this.queue.length) return;
    // More than a few seconds in, "previous" means "start this track over".
    if (this._audio.currentTime > 3) { this.seek(0); return; }

    this._resumeAt = 0;
    if (this._pos > 0) { this._goto(this._pos - 1); return; }
    if (this.repeat === 'all' && this._order.length) { this._goto(this._order.length - 1); return; }
    this.seek(0);
  },

  _goto(pos) {
    if (pos < 0 || pos >= this._order.length) return;
    this._pos = pos;
    this.index = this._order[pos];
    this._errors = 0;
    this.play();
  },

  /** Advance without the 'end of queue' toast - used when skipping failures. */
  _skip(step) {
    if (!this.queue.length) return;
    const target = this._pos + step;
    if (target < 0 || target >= this._order.length) { this.stop(); return; }
    this._pos = target;
    this.index = this._order[target];
    this._errors = 0;
    this.play();
  },

  seek(seconds) {
    if (!Number.isFinite(seconds)) return;
    const dur = this._audio.duration;
    const t = Math.max(0, dur && Number.isFinite(dur) ? Math.min(seconds, dur - 0.25) : seconds);
    this._audio.currentTime = t;
    this.render();
  },

  nudge(delta) {
    this.seek(this._audio.currentTime + delta);
  },

  setVolume(v) {
    this.volume = Math.min(1, Math.max(0, Number(v) || 0));
    // Moving the slider off zero is an unmute, which is what people expect.
    if (this.volume > 0 && this.muted) this.muted = false;
    this._applyVolume();
    this.save();
    this.render();
  },

  toggleMute() {
    this.muted = !this.muted;
    this._applyVolume();
    this.save();
    this.render();
  },

  _applyVolume() {
    this._audio.volume = this.muted ? 0 : this.volume;
    this._audio.muted = this.muted;
  },

  toggleShuffle() {
    this.shuffle = !this.shuffle;
    this.rebuildOrder();
    this.save();
    this.render();
  },

  cycleRepeat() {
    const order = ['off', 'all', 'one'];
    this.repeat = order[(order.indexOf(this.repeat) + 1) % order.length];
    this.save();
    this.render();
  },

  toggleQueue(force) {
    this.queueOpen = force === undefined ? !this.queueOpen : !!force;
    this._el.queue.hidden = !this.queueOpen;
    this._el.queueToggle.setAttribute('aria-expanded', String(this.queueOpen));
    this.save();
    if (this.queueOpen) this.renderQueue();
  },

  _onError() {
    const track = this.current();
    this._errors += 1;
    if (this._errors >= MAX_CONSECUTIVE_ERRORS || this._order.length <= 1) {
      showToast(`Could not play "${track ? track.title : 'track'}"`, 'error');
      this.stop();
      this.save();
      return;
    }
    this._skip(1);
  },

  // ---------------------------------------------------------------------
  // Wiring
  // ---------------------------------------------------------------------

  wire() {
    const a = this._audio;
    const e = this._el;

    a.addEventListener('play', () => this.render());
    a.addEventListener('pause', () => this.render());
    a.addEventListener('loadedmetadata', () => this.render());
    a.addEventListener('durationchange', () => this.render());
    a.addEventListener('timeupdate', () => {
      // Only repaint the bar while playing; when paused, `render` owns the
      // position display and a late timeupdate would fight the user.
      if (!a.paused && !this._scrubbing) this.renderProgress();
    });
    a.addEventListener('ended', () => {
      if (this.repeat === 'one') {
        a.currentTime = 0;
        a.play().catch(() => this._onError());
        return;
      }
      this.next(false);
    });
    a.addEventListener('error', () => {
      // Ignore the error event fired by clearing src in stop().
      if (!a.getAttribute('src')) return;
      this._onError();
    });

    e.play.addEventListener('click', () => this.toggle());
    e.prev.addEventListener('click', () => this.prev());
    e.next.addEventListener('click', () => this.next());
    e.shuffle.addEventListener('click', () => this.toggleShuffle());
    e.repeat.addEventListener('click', () => this.cycleRepeat());
    e.mute.addEventListener('click', () => this.toggleMute());
    e.queueToggle.addEventListener('click', () => this.toggleQueue());
    e.close.addEventListener('click', () => this.toggleQueue(false));
    e.clear.addEventListener('click', () => this.clearQueue());

    e.volume.addEventListener('input', () => this.setVolume(e.volume.value));

    // Scrubbing: while the pointer is down the bar follows the drag, not the
    // audio. Releasing seeks once, so a drag does not fire a seek per pixel.
    const wasPlaying = () => !a.paused;
    let resumeAfter = false;
    e.progress.addEventListener('pointerdown', () => {
      this._scrubbing = true;
      resumeAfter = wasPlaying();
    });
    e.progress.addEventListener('input', () => {
      const dur = a.duration;
      if (!dur || !Number.isFinite(dur)) return;
      const t = (Number(e.progress.value) / 1000) * dur;
      e.elapsed.textContent = fmtDuration(t);
    });
    const endScrub = () => {
      if (!this._scrubbing) return;
      this._scrubbing = false;
      const dur = a.duration;
      if (dur && Number.isFinite(dur)) {
        a.currentTime = (Number(e.progress.value) / 1000) * dur;
      }
      if (resumeAfter) a.play().catch(() => {});
      this.render();
    };
    e.progress.addEventListener('pointerup', endScrub);
    e.progress.addEventListener('pointercancel', endScrub);
    e.progress.addEventListener('keyup', endScrub);
    e.progress.addEventListener('change', endScrub);

    // Persist the position periodically rather than on every timeupdate:
    // writing to localStorage 4x a second would be its own kind of jank.
    setInterval(() => { if (this.current() && !this._audio.paused) this.save(); }, 5000);
    window.addEventListener('beforeunload', () => this.save());
  },

  _updateMediaSession(track) {
    // Hardware media keys (headphone buttons, the OS volume rocker) route
    // here rather than to the page's key handler.
    if (!('mediaSession' in navigator) || !navigator.mediaSession.setActionHandler) return;
    const set = (action, fn) => {
      try { navigator.mediaSession.setActionHandler(action, fn); } catch { /* unsupported action */ }
    };
    navigator.mediaSession.metadata = new MediaMetadata({
      title: track.title || '',
      artist: track.artist || '',
      album: track.album || '',
      artwork: [{ src: `/api/library/tracks/${track.id}/cover`, sizes: '256x256' }],
    });
    set('play', () => this.play());
    set('pause', () => this.pause());
    set('previoustrack', () => this.prev());
    set('nexttrack', () => this.next());
  },

  // ---------------------------------------------------------------------
  // Keyboard
  // ---------------------------------------------------------------------

  setupKeys() {
    document.addEventListener('keydown', (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      // A modal is up and wants the keyboard for itself.
      if (this.suspended) return;
      // Never steal a keystroke from a field the user is typing into.
      const el = e.target;
      if (el) {
        const tag = (el.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable) return;
      }
      if (!this.queue.length) return;

      const key = e.key.toLowerCase();
      const hit = () => { e.preventDefault(); };

      switch (key) {
        case ' ': hit(); this.toggle(); break;
        case 'arrowright': hit(); this.nudge(SEEK_STEP); break;
        case 'arrowleft': hit(); this.nudge(-SEEK_STEP); break;
        case 'arrowup': hit(); this.setVolume(this.volume + VOLUME_STEP); break;
        case 'arrowdown': hit(); this.setVolume(this.volume - VOLUME_STEP); break;
        case 'n': hit(); this.next(); break;
        case 'p': hit(); this.prev(); break;
        case 's': hit(); this.toggleShuffle(); break;
        case 'r': hit(); this.cycleRepeat(); break;
        case 'm': hit(); this.toggleMute(); break;
        case 'q': hit(); this.toggleQueue(); break;
        default: break;
      }
    });
  },

  // ---------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------

  render() {
    const e = this._el;
    if (!e.root) return;
    const track = this.current();
    const a = this._audio;

    e.root.classList.toggle('is-empty', !track);

    if (track) {
      e.art.innerHTML = coverImg(track, 'pb-art-img');
      e.title.textContent = track.title || 'Unknown track';
      e.artist.textContent = [track.artist, track.album].filter(Boolean).join(' — ')
        || 'Unknown artist';
      document.title = `${track.title || 'Soundloom'} · Soundloom`;
    } else {
      e.art.innerHTML = '';
      e.title.textContent = 'Nothing playing';
      e.artist.textContent = 'Pick something from your library';
      document.title = 'Soundloom';
    }

    const playing = !!track && !a.paused;
    e.playIcon.innerHTML = `<path d="${playing ? PAUSE_PATH : PLAY_PATH}"/>`;
    e.play.setAttribute('aria-label', playing ? 'Pause' : 'Play');
    e.play.title = `${playing ? 'Pause' : 'Play'} (Space)`;
    e.play.disabled = !track;

    e.shuffle.classList.toggle('active', this.shuffle);
    e.shuffle.setAttribute('aria-pressed', String(this.shuffle));
    e.repeat.classList.toggle('active', this.repeat !== 'off');
    e.repeat.setAttribute('aria-pressed', String(this.repeat !== 'off'));
    e.repeatBadge.hidden = this.repeat !== 'one';
    e.repeat.title = `Repeat ${this.repeat} (R)`;
    e.volume.value = String(this.muted ? 0 : this.volume);
    e.mute.title = this.muted ? 'Unmute (M)' : 'Mute (M)';
    this._paintVolumeIcon();

    this.renderProgress();
    if (this.queueOpen) this.renderQueue();
  },

  renderProgress() {
    const e = this._el;
    if (!e.progress) return;
    const a = this._audio;
    const dur = a.duration;
    const known = dur && Number.isFinite(dur) && dur > 0;
    // Prefer the element's duration, because a freshly-resumed session gets
    // its real length from the file rather than from the stored metadata.
    const total = known ? dur : (this.current() ? this.current().duration : 0);
    const now = a.currentTime || 0;

    if (!this._scrubbing) {
      e.progress.value = String(known ? Math.round((now / dur) * 1000) : 0);
    }
    e.elapsed.textContent = fmtDuration(now);
    e.duration.textContent = fmtDuration(total);
  },

  _paintVolumeIcon() {
    const e = this._el;
    const v = this.muted ? 0 : this.volume;
    const waves = v > 0.5
      ? '<path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>'
      : v > 0
        ? '<path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>'
        : '';
    e.volumeIcon.innerHTML =
      `<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>${waves}`;
  },

  renderQueue() {
    const e = this._el;
    if (!this.queueOpen || !e.queueList) return;

    if (!this.queue.length) {
      e.queueList.innerHTML = '<li class="pq-empty">Nothing queued yet</li>';
      e.queueCount.textContent = '';
      return;
    }

    const total = this.queue.length;
    const remaining = total - this._pos;
    e.queueCount.textContent = `${total} track${total === 1 ? '' : 's'}`
      + (this.index >= 0 ? ` · ${remaining} left` : '');

    e.queueList.innerHTML = this.queue.map((t, i) => `
      <li class="pq-item ${i === this.index ? 'current playing' : ''}" data-q="${i}">
        <span class="pq-index">${i + 1}</span>
        <div class="pq-item-main">
          <div class="pq-item-title">${esc(t.title)}</div>
          <div class="pq-item-artist">${esc(t.artist || '')}</div>
        </div>
        <span class="pq-item-dur">${fmtDuration(t.duration)}</span>
        <button type="button" class="pq-item-remove" data-remove="${i}" title="Remove" aria-label="Remove ${esc(t.title)}">&#10005;</button>
      </li>`).join('');

    e.queueList.querySelectorAll('.pq-item').forEach(row => {
      row.addEventListener('click', (ev) => {
        if (ev.target.dataset.remove !== undefined) {
          this.removeAt(Number(ev.target.dataset.remove));
          return;
        }
        const i = Number(row.dataset.q);
        this._pos = this._order.indexOf(i);
        if (this._pos < 0) { this.rebuildOrder(); this._pos = this._order.indexOf(i); }
        this.index = i;
        this._errors = 0;
        this.play();
      });
    });

    // Keep the playing row in view when the queue is long.
    const current = e.queueList.querySelector('.pq-item.playing');
    if (current) current.scrollIntoView({ block: 'nearest' });
  },
};

// Convenience for the inline handlers in index.html and the tab modules.
Object.assign(window, { Player });
