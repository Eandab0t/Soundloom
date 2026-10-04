/* Soundloom Needs Attention — one list of things waiting on a decision.

   The backend decides what each row can do: every item carries an `action`
   object naming its own routes, so this file renders whatever is available
   rather than switching on `type` and hardcoding a mapping that drifts out of
   date the next time a producer is added.

   A blocked download is the case that has to be decidable *here*. The match
   gate refused a candidate, and the three answers are genuinely different:

     Accept   - the candidate is right and the score is being pedantic
     Reject   - wrong track entirely; stop
     Re-point - the *request* was wrong; correct it and search again

   Accept and Re-point look similar and are not. Accept takes this file; a
   re-point goes back through the matcher and can be blocked again. The UI
   says so, because "search again" reading as "download anyway" would quietly
   turn the gate off. */
import { API, State, showToast, esc } from '../core.js';

const TYPE_LABELS = {
  blocked_download: 'Blocked download',
  interrupted_operation: 'Interrupted',
  metadata_review: 'Needs tags',
};

/* What each decision actually did, in the user's terms rather than the
   backend's verb. */
const DONE = {
  accept: 'Accepted — downloading that candidate even though it scored low.',
  reject: 'Rejected — wrong track. This download will not be retried.',
  repoint: 'Re-pointed — searching again with the corrected request.',
  dismiss: 'Dismissed.',
  reopen: 'Reopened.',
};

export const Attention = {
  items: [],
  counts: {},
  severity: {},
  filter: 'open',    // open | all
  busy: new Set(),   // item ids with a request in flight

  async load() {
    try {
      const data = await API.get(`/api/attention?status=${this.filter}&limit=200`);
      this.items = data.items || [];
      this.counts = data.counts || {};
      this.severity = data.severity_counts || {};
      this.render();
    } catch (e) { console.error(e); }
  },

  async refresh() {
    // Scan first so a download that just got blocked appears without waiting
    // for the scheduled run, then redraw from the scan's own result.
    try {
      await API.post('/api/attention/refresh', {});
    } catch { /* the list below is still worth showing */ }
    await this.load();
    this.updateBadge();
  },

  setFilter(which) {
    this.filter = which;
    document.querySelectorAll('.seg-btn[data-att-filter]').forEach(b =>
      b.classList.toggle('active', b.dataset.attFilter === which));
    this.load();
  },

  async updateBadge() {
    try {
      const data = await API.get('/api/attention/counts?status=open');
      const n = (data.counts && data.counts.total) || 0;
      const badge = document.getElementById('attention-badge');
      if (badge) {
        badge.textContent = n > 99 ? '99+' : String(n);
        badge.style.display = n ? '' : 'none';
      }
      const head = document.getElementById('attention-summary');
      if (head && State.get('currentTab') === 'attention') {
        head.textContent = n
          ? `${n} thing${n === 1 ? '' : 's'} ${n === 1 ? 'needs' : 'need'} a decision.`
          : 'Nothing needs a decision.';
      }
    } catch { /* server down */ }
  },

  render() {
    const el = document.getElementById('attention-list');
    if (!el) return;

    const n = this.counts.total || 0;
    const summary = document.getElementById('attention-summary');
    if (summary) {
      summary.textContent = this.filter === 'open'
        ? (n ? `${n} thing${n === 1 ? '' : 's'} ${n === 1 ? 'needs' : 'need'} a decision.`
              : 'Nothing needs a decision.')
        : `Including resolved and dismissed items (${this.items.length} total).`;
    }

    if (!this.items.length) {
      el.innerHTML = `<div class="empty-state">
        <h3>All clear</h3>
        <p>${this.filter === 'open'
          ? 'Blocked downloads, interrupted transfers and low-confidence tags will show up here.'
          : 'Nothing has needed attention yet.'}</p>
      </div>`;
      return;
    }

    el.innerHTML = this.items.map(i => this._row(i)).join('');
    this._wire(el);
  },

  _row(item) {
    const a = item.action || {};
    const kind = a.kind || item.type;
    const busy = this.busy.has(item.id);
    const open = item.status === 'open';

    return `
      <div class="att-row" data-id="${item.id}">
        <div class="item-status att-${esc(item.severity || 'medium')}"></div>
        <div class="att-body">
          <div class="att-head">
            <span class="att-type">${esc(TYPE_LABELS[item.type] || item.type)}</span>
            <span class="muted">seen ${item.seen_count || 1}×</span>
            ${open ? '' : `<span class="tag">${esc(item.status)}</span>`}
          </div>
          <div class="att-title">${esc(item.title || '')}</div>
          ${item.description ? `<div class="att-desc">${esc(item.description)}</div>` : ''}
          ${kind === 'blocked_download' ? this._compare(a) : ''}
          ${this._actions(item, a, busy, open)}
        </div>
      </div>`;
  },

  /* The two sides of the comparison, side by side. This is the whole reason
     to show the row at all: "you wanted X, the source returned Y, it scored
     N against a threshold of T" is a decision, whereas the job's error string
     is a paragraph the user has to interpret. */
  _compare(a) {
    const score = Math.round(a.score ?? 0);
    const threshold = a.threshold ?? 0;
    return `
      <div class="att-compare">
        <div class="att-side">
          <span class="att-side-label">You asked for</span>
          <span class="att-side-value">${esc(a.wanted_artist || '—')}</span>
          <span class="att-side-sub">${esc(a.wanted_title || '')}</span>
        </div>
        <div class="att-score" title="Match confidence against the threshold">
          <span class="att-score-num">${score}%</span>
          <span class="att-score-thr">of ${threshold}%</span>
        </div>
        <div class="att-side">
          <span class="att-side-label">Source returned</span>
          <span class="att-side-value">${esc(a.candidate_artist || '—')}</span>
          <span class="att-side-sub">${esc(a.candidate_title || '')}</span>
        </div>
      </div>
      ${a.explanation ? `<div class="att-why">${esc(a.explanation)}</div>` : ''}`;
  },

  _actions(item, a, busy, open) {
    const id = item.id;
    const off = busy ? 'disabled' : '';
    if (!open) {
      return `<div class="att-actions">
        <button class="btn ghost sm" data-act="reopen" data-id="${id}"
          ${off}>Reopen</button></div>`;
    }

    const buttons = [];
    if (a.accept_route) {
      buttons.push(`<button class="btn primary sm" data-act="accept" data-id="${id}"
        ${off} title="Download this candidate even though it scored ${Math.round(a.score ?? 0)}%">Accept this one</button>`);
    }
    if (a.reject_route) {
      buttons.push(`<button class="btn ghost sm" data-act="reject" data-id="${id}"
        ${off} title="Wrong track. This download will not be retried.">Reject</button>`);
    }
    if (a.repoint_route) {
      buttons.push(`<button class="btn ghost sm" data-act="repoint" data-id="${id}"
        ${off} title="Correct what you asked for and search again. The match check still applies.">Re-point…</button>`);
    }

    // Interrupted operations bring their own two answers, named by the
    // producer. Using them is the whole point of the action block: the row
    // that reported the problem is where it gets decided.
    if (a.retry_route) {
      buttons.push(`<button class="btn ghost sm" data-act="opRetry" data-id="${id}"
        ${off}>${esc(a.retry_label || 'Retry')}</button>`);
    }
    if (a.discard_route) {
      buttons.push(`<button class="btn ghost sm" data-act="opDiscard" data-id="${id}"
        ${off}>${esc(a.discard_label || 'Discard')}</button>`);
    }

    // Anything with no actions of its own still gets its review link.
    if (!buttons.length && a.route) {
      buttons.push(`<a class="btn ghost sm" href="#${esc(String(a.route).replace(/^\//, ''))}"
        data-goto="${esc(a.route)}">${esc(a.label || 'Review')}</a>`);
    }

    buttons.push(`<button class="btn ghost sm" data-act="dismiss" data-id="${id}"
      ${off} title="Not worth acting on">Dismiss</button>`);

    const repointForm = a.repoint_route ? `
      <div class="att-repoint" data-for="${id}" hidden>
        <label class="att-field">
          <span>Artist</span>
          <input type="text" class="att-input" data-f="artist"
            value="${esc(a.wanted_artist || '')}" placeholder="Artist">
        </label>
        <label class="att-field">
          <span>Title</span>
          <input type="text" class="att-input" data-f="title"
            value="${esc(a.wanted_title || '')}" placeholder="Title">
        </label>
        <div class="att-repoint-actions">
          <button class="btn primary sm" data-act="repointGo" data-id="${id}">Search again</button>
          <button class="btn ghost sm" data-act="repointCancel" data-id="${id}">Cancel</button>
        </div>
        <p class="muted att-repoint-note">This corrects what you asked for — it does not lower
          the bar. The result is match-checked again before anything downloads.</p>
      </div>` : '';

    return `<div class="att-actions">${buttons.join('')}</div>${repointForm}`;
  },

  _wire(el) {
    el.querySelectorAll('[data-act]').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        // data-act is an action name, resolved through HANDLERS rather than
        // `this[name]` so a typo is a loud console error, not a dead button.
        const fn = HANDLERS[btn.dataset.act];
        if (!fn) { console.error('No handler for', btn.dataset.act); return; }
        fn.call(this, parseInt(btn.dataset.id), btn);
      });
    });
    el.querySelectorAll('[data-goto]').forEach(a =>
      a.addEventListener('click', (e) => {
        e.preventDefault();
        location.hash = a.dataset.goto;
      }));
  },

  async _call(id, url, body, verb, btn) {
    if (this.busy.has(id)) return;
    this.busy.add(id);
    if (btn) btn.disabled = true;
    try {
      await API.post(url, body);
      showToast(DONE[verb] || 'Done', 'success');
      this.busy.delete(id);
      await this.load();
      this.updateBadge();
    } catch (e) {
      showToast(e.message, 'error', 7000);
      this.busy.delete(id);
      if (btn) btn.disabled = false;
    }
  },

  accept(id, btn) {
    const route = this._route(id, 'accept_route');
    if (route) this._call(id, route, { note: 'Accepted from Needs Attention' }, 'accept', btn);
  },

  reject(id, btn) {
    const route = this._route(id, 'reject_route');
    if (route) this._call(id, route, { note: 'Rejected from Needs Attention' }, 'reject', btn);
  },

  repoint(id, btn) {
    const panel = document.querySelector(`.att-repoint[data-for="${id}"]`);
    if (!panel) return;
    panel.hidden = false;
    if (btn) btn.disabled = true;
    panel.querySelector('[data-f="title"]')?.focus();
  },

  repointCancel(id) {
    const panel = document.querySelector(`.att-repoint[data-for="${id}"]`);
    if (panel) panel.hidden = true;
    const btn = document.querySelector(`.att-row[data-id="${id}"] [data-act="repoint"]`);
    if (btn) btn.disabled = false;
  },

  async repointGo(id, btn) {
    const route = this._route(id, 'repoint_route');
    if (!route) return;
    const panel = document.querySelector(`.att-repoint[data-for="${id}"]`);
    const artist = (panel?.querySelector('[data-f="artist"]')?.value || '').trim();
    const title = (panel?.querySelector('[data-f="title"]')?.value || '').trim();
    if (!artist && !title) {
      showToast('Enter an artist or a title to search for', 'info');
      return;
    }
    await this._call(id, route, { artist, title }, 'repoint', btn);
  },

  /* Retry / discard for an interrupted operation. The producer stops
     reporting the operation once it leaves failed/abandoned, so its attention
     row would retire on the next scan - but closing it here means the list
     reflects the decision immediately rather than a scan later. */
  async opRetry(id, btn) {
    const route = this._route(id, 'retry_route');
    if (!route) return;
    await this._opAct(id, route, 'Retrying.');
  },

  async opDiscard(id, btn) {
    const route = this._route(id, 'discard_route');
    if (!route) return;
    await this._opAct(id, route, 'Discarded.');
  },

  async _opAct(id, route, message) {
    if (this.busy.has(id)) return;
    this.busy.add(id);
    const btn = document.querySelector(`.att-row[data-id="${id}"]`);
    if (btn) btn.querySelectorAll('button').forEach(b => { b.disabled = true; });
    try {
      await API.post(route, {});
      await API.post(`/api/attention/${id}/resolve`, { note: message });
      showToast(message, 'success');
      this.busy.delete(id);
      await this.load();
      this.updateBadge();
    } catch (e) {
      showToast(e.message, 'error', 7000);
      this.busy.delete(id);
    }
  },

  dismiss(id, btn) {
    this._call(id, `/api/attention/${id}/dismiss`, { note: 'Dismissed' }, 'dismiss', btn);
  },

  reopen(id, btn) {
    this._call(id, `/api/attention/${id}/reopen`, {}, 'reopen', btn);
  },

  /* The route the backend said this item has. Missing means the backend and
     this screen disagree, which is worth saying out loud rather than
     silently doing nothing. */
  _route(id, key) {
    const item = this.items.find(i => i.id === id);
    const route = item && item.action ? item.action[key] : null;
    if (!route) console.error('No', key, 'on attention item', id);
    return route;
  },
};

const HANDLERS = {
  accept: Attention.accept,
  reject: Attention.reject,
  repoint: Attention.repoint,
  repointGo: Attention.repointGo,
  repointCancel: Attention.repointCancel,
  opRetry: Attention.opRetry,
  opDiscard: Attention.opDiscard,
  dismiss: Attention.dismiss,
  reopen: Attention.reopen,
};
