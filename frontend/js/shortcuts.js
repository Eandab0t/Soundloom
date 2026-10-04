/* Soundloom keyboard shortcuts help sheet.

   Every player binding was invisible until you happened to guess it, and the
   mouse gestures that make a queue usable - Shift-click to play from here,
   Shift-click + to jump the line - were the least discoverable part of the
   player by a wide margin. This puts the whole set in one place.

   Two things are load-bearing:

   **The player stops listening while the sheet is open.** Its bindings are on
   `document`, and the sheet contains no focusable text fields for the usual
   input guard to catch. Without an explicit suspension, pressing Space to
   scroll this panel would pause the track behind it, and Q would yank the
   queue open underneath. See `Player.suspended`.

   **The list is written out, not generated from the handler.** A sheet built by
   introspecting the key handler would rot the moment a binding is added and
   forgotten - which is the failure this is meant to prevent. So the
   correspondence is pinned from the other side instead:
   tests/test_shortcuts_sheet.py reads this file and player.js as text and
   fails if either documents or handles a key the other does not. (That test is
   why "Shuffle" and "Repeat" are in the Playback group - it caught their
   absence the first time it ran.) */
import { Player } from './player.js';

/* The documented set. `keys` are display strings; they are deliberately not
   parsed - one row can show a chord, or a gesture rather than a key.

   Two flags mark what a row is *not*, because absence is ambiguous:
     gesture: true  - a mouse gesture or on-screen button, not a key
     system: true   - a shell/sheet binding, not a player transport key

   Every other row is a player binding and is checked against setupKeys() by
   tests/test_shortcuts_sheet.py, in both directions. */
export const SHORTCUT_GROUPS = [
  {
    title: 'Playback',
    items: [
      { keys: ['Space'], label: 'Play / pause' },
      { keys: ['←', '→'], label: 'Seek back / forward 5 seconds' },
      { keys: ['↑', '↓'], label: 'Volume up / down' },
      { keys: ['M'], label: 'Mute / unmute' },
      { keys: ['S'], label: 'Shuffle — keep the queue, change the play order' },
      { keys: ['R'], label: 'Repeat — cycles off, all, then one' },
    ],
  },
  {
    title: 'Tracks',
    items: [
      { keys: ['N'], label: 'Next track' },
      { keys: ['P'], label: 'Previous track' },
    ],
  },
  {
    title: 'Queue',
    /* Mouse gestures, not keys - rendered in a flatter chip so a key and a
       button never look like the same kind of thing to press. */
    items: [
      { keys: ['Q'], label: 'Show / hide the queue' },
      { keys: ['Clear queue'], label: 'Empty the queue without stopping playback', gesture: true },
      { keys: ['Click', '+'], label: 'Add the track to the end of the queue', gesture: true },
      { keys: ['Shift', '+ Click', '+'], label: 'Play next — insert ahead of the current track', gesture: true },
      { keys: ['Click', '▶'], label: 'Play just this track', gesture: true },
      { keys: ['Shift', 'Click', '▶'], label: 'Play from this track onward', gesture: true },
      { keys: ['Click', 'queue row'], label: 'Play that queue entry straight away', gesture: true },
      { keys: ['Click', '✕'], label: 'Drop that track from the queue', gesture: true },
    ],
  },
  {
    title: 'Navigation',
    /* Not player bindings - these belong to the shell and to this sheet, so
       they are flagged out of the correspondence check against setupKeys().
       Without the flag a stray key here would be read as a player shortcut
       that does not exist. */
    items: [
      { keys: ['Ctrl', 'K'], label: 'Jump to the library search box', system: true },
      { keys: ['?'], label: 'Open or close this sheet', system: true },
      { keys: ['Esc'], label: 'Close this sheet', system: true },
    ],
  },
];

export const Shortcuts = {
  open_: false,
  lastFocus: null,

  init() {
    this.render();
    this.wire();
  },

  render() {
    const body = document.getElementById('shortcuts-body');
    if (!body) return;
    body.innerHTML = SHORTCUT_GROUPS.map(g => `
      <section class="sc-group">
        <h4 class="sc-title">${g.title}</h4>
        <dl class="sc-list">
          ${g.items.map(it => {
            // Chips sit side by side with no separator. A word between them
            // made chords and gestures unreadable ("CtrlorK").
            const cls = it.gesture ? 'sc-gesture' : 'sc-key';
            const tag = it.gesture ? 'span' : 'kbd';
            const chips = it.keys.map(k => `<${tag} class="${cls}">${k}</${tag}>`).join('');
            return `<div class="sc-row">
              <dt class="sc-keys">${chips}</dt>
              <dd class="sc-label">${it.label}</dd>
            </div>`;
          }).join('')}
        </dl>
      </section>`).join('')
      + `<p class="sc-foot muted">Player shortcuts are ignored while you are
         typing in a field, while this sheet is open, and when nothing is
         queued.</p>`;
  },

  wire() {
    const sheet = document.getElementById('shortcuts-sheet');
    if (!sheet) return;

    // Backdrop click closes. `e.target === sheet` rather than a listener on the
    // backdrop itself, so a click inside the card does not bubble into a close.
    sheet.addEventListener('click', (e) => {
      if (e.target === sheet) this.close();
    });
    sheet.querySelector('[data-close]')?.addEventListener('click', () => this.close());

    document.querySelectorAll('[data-open-shortcuts]').forEach(btn =>
      btn.addEventListener('click', () => this.toggle()));

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.open_) {
        e.preventDefault();
        this.close();
        return;
      }
      if (e.key !== '?') return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      // Same rule the player uses: never steal a keystroke from a field.
      const el = e.target;
      if (el) {
        const tag = (el.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable) return;
      }
      e.preventDefault();
      this.toggle();
    });
  },

  toggle() { this.open_ ? this.close() : this.open(); },

  open() {
    const sheet = document.getElementById('shortcuts-sheet');
    if (!sheet || this.open_) return;
    this.lastFocus = document.activeElement;
    this.open_ = true;
    // Hide the player's bindings for as long as this is up - see the file header.
    Player.suspended = true;
    sheet.hidden = false;
    sheet.querySelector('[data-close]')?.focus();
  },

  close() {
    const sheet = document.getElementById('shortcuts-sheet');
    if (!sheet || !this.open_) return;
    this.open_ = false;
    Player.suspended = false;
    sheet.hidden = true;
    // Send focus back where it came from, so a keyboard user is not dropped at
    // the top of the document.
    if (this.lastFocus && this.lastFocus.isConnected) this.lastFocus.focus();
    this.lastFocus = null;
  },
};
