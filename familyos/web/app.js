/* FamilyOS console.
 *
 * Served by the API it talks to, so a request is same-origin and the member's
 * bearer token goes straight on it: no CORS rule, no second host.
 *
 * The one idea this console is built around: a claim shows the words it was
 * read from, and where they sit in the original. Claims carry a page, a
 * character span and a box per line, so selecting a claim draws those boxes
 * over the rendered page. A claim the grounder could not place says so, and
 * never became an obligation.
 */
'use strict';

const PDFJS_VERSION = '3.11.174';
const PDFJS_BASE = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/' + PDFJS_VERSION;

const root = document.getElementById('root');
const store = {
  token: null,
  me: null,
  household: null,
  mailboxes: [],
  quarantineCount: 0,
  amendmentCount: 0,
};

/* ── plumbing ─────────────────────────────────────────────────── */

function readToken() {
  try { return localStorage.getItem('familyos.token'); } catch (e) { return null; }
}
function writeToken(value) {
  try {
    if (value) localStorage.setItem('familyos.token', value);
    else localStorage.removeItem('familyos.token');
  } catch (e) { /* private window: the session simply does not outlive the tab */ }
}

class ApiError extends Error {
  constructor(status, body) {
    super((body && (body.detail || body.error)) || ('HTTP ' + status));
    this.status = status;
    this.body = body || {};
  }
}

async function api(path, options) {
  const opts = options || {};
  const headers = Object.assign({}, opts.headers);
  if (store.token) headers.Authorization = 'Bearer ' + store.token;
  let body = opts.body;
  if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.json);
  }
  const res = await fetch('/v1' + path, { method: opts.method || 'GET', headers, body });
  if (res.status === 204) return null;
  const raw = opts.blob ? null : await res.text();
  if (!res.ok) {
    let parsed = null;
    try { parsed = raw ? JSON.parse(raw) : null; } catch (e) { parsed = { detail: raw }; }
    if (res.status === 401 && store.token) { forget(); }
    throw new ApiError(res.status, parsed);
  }
  if (opts.blob) return res.blob();
  return raw ? JSON.parse(raw) : null;
}

function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

let flashTimer = null;
function flash(message, bad) {
  const old = document.querySelector('.flash');
  if (old) old.remove();
  if (flashTimer) clearTimeout(flashTimer);
  const el = document.createElement('div');
  el.className = 'flash' + (bad ? ' bad' : '');
  el.setAttribute('role', 'status');
  el.textContent = message;
  document.body.appendChild(el);
  flashTimer = setTimeout(() => el.remove(), 5200);
}

function fail(err) {
  const extra = err.body && err.body.member_ids ? ' (' + err.body.member_ids.length + ' child without consent)' : '';
  flash(err.message + extra, true);
}

const DAY = { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' };
function date(iso) {
  if (!iso) return '';
  const d = new Date(iso.length <= 10 ? iso + 'T00:00:00' : iso);
  if (isNaN(d)) return iso;
  return d.toLocaleDateString(undefined, DAY);
}
function ago(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + 'm ago';
  if (mins < 1440) return Math.round(mins / 60) + 'h ago';
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}
function bytes(n) {
  if (!n && n !== 0) return '';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return Math.round(n / 1024) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}
function money(value, currency) {
  if (value === null || value === undefined) return '';
  const symbol = { INR: '₹', USD: '$', GBP: '£', EUR: '€' }[currency];
  const n = Number(value);
  const shown = Number.isInteger(n) ? String(n) : n.toFixed(2);
  return symbol ? symbol + shown : shown + (currency ? ' ' + currency : '');
}
function initial(name) { return (name || '?').trim().charAt(0).toUpperCase(); }

function memberName(id) {
  if (!id || !store.household) return '';
  const m = (store.household.members || []).find((x) => x.id === id);
  return m ? m.display_name : '';
}
function membersByRole(roles) {
  if (!store.household) return [];
  return (store.household.members || []).filter((m) => roles.indexOf(m.role) !== -1);
}

/* ── routing ──────────────────────────────────────────────────── */

const ROUTES = [
  { path: /^\/notices\/([0-9a-f-]{36})$/i, view: noticeView },
  { path: /^\/notices$/, view: noticesView, nav: 'notices' },
  { path: /^\/today$/, view: todayView, nav: 'today' },
  { path: /^\/quarantine$/, view: quarantineView, nav: 'quarantine' },
  { path: /^\/revisions$/, view: amendmentsView, nav: 'revisions' },
  { path: /^\/household$/, view: householdView, nav: 'household' },
  { path: /^\/audit$/, view: auditView, nav: 'audit' },
];

/* The Google callback comes back to #/household?mailbox=connected, so a path
   has to survive having a query on it. */
function here() {
  const raw = location.hash.replace(/^#/, '').split('?')[0];
  return raw || '/notices';
}
function hashQuery() {
  return new URLSearchParams(location.hash.replace(/^#/, '').split('?')[1] || '');
}
function go(path) { location.hash = path; }

async function render() {
  if (!store.token) { gateView(); return; }
  const path = here();
  const match = ROUTES.map((r) => ({ r, m: path.match(r.path) })).find((x) => x.m);
  const route = match ? match.r : ROUTES[1];
  const args = match ? match.m.slice(1) : [];

  root.innerHTML = chrome(route.nav) + '<main><p class="muted">Loading&hellip;</p></main>';
  wireChrome();
  const main = root.querySelector('main');
  try {
    await route.view(main, args);
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return;
    main.innerHTML = '<div class="card pad"><h2>That did not load</h2><p class="muted" style="margin-top:8px">'
      + esc(err.message) + '</p></div>';
  }
}

/* ── chrome ───────────────────────────────────────────────────── */

function chrome(active) {
  const isGuardian = store.me && store.me.role === 'guardian';
  const link = (id, label, extra) =>
    '<a href="#/' + id + '"' + (active === id ? ' aria-current="page"' : '') + '>' + label + (extra || '') + '</a>';
  const badge = store.quarantineCount ? ' <span class="count">' + store.quarantineCount + '</span>' : '';
  const revBadge = store.amendmentCount ? ' <span class="count amber">' + store.amendmentCount + '</span>' : '';
  return '<header class="top">'
    + '<div class="brand"><b>FamilyOS</b><span>' + esc(store.household ? store.household.name : '') + '</span></div>'
    + '<nav class="nav">'
    + link('notices', 'Notices')
    + link('today', 'Today')
    + (isGuardian ? link('quarantine', 'Quarantine', badge) : '')
    + link('revisions', 'Revisions', revBadge)
    + link('household', 'Household')
    + (isGuardian ? link('audit', 'Audit') : '')
    + '</nav>'
    + '<div class="who"><span>' + esc(store.me ? store.me.display_name : '') + '</span>'
    + '<span class="avatar">' + esc(initial(store.me && store.me.display_name)) + '</span>'
    + '<button class="btn ghost sm" data-signout>Sign out</button></div>'
    + '</header>';
}

function wireChrome() {
  const btn = root.querySelector('[data-signout]');
  if (btn) btn.addEventListener('click', () => { signOut().catch(() => forget()); });
}

function forget() {
  store.token = null;
  store.me = null;
  store.household = null;
  writeToken(null);
  gateView();
}

async function signOut() {
  // End the token on the server first. Clearing localStorage alone only
  // forgets it in this browser: it would keep working for anyone holding it.
  try {
    if (store.token) await api('/signout', { method: 'POST' });
  } catch (e) { /* already gone, or the server cannot be reached */ }
  forget();
}

/* ── sign in ──────────────────────────────────────────────────── */

function gateView() {
  root.innerHTML = '<div class="gate">'
    + '<div><div class="mark">FamilyOS</div>'
    + '<p class="lede" style="margin-top:10px">School notices, bills and forms turned into things that get done &mdash; '
    + 'without losing track of where each fact came from, or who is allowed to see it.</p></div>'
    + '<div class="tabs"><button data-tab="in" aria-pressed="true">Use a token</button>'
    + '<button data-tab="new" aria-pressed="false">Start a household</button></div>'
    + '<div class="card pad" data-panel="in">'
    + '<form class="stack" data-form="in">'
    + '<label>Sign-in token<input type="password" name="token" placeholder="fos_&hellip;" autocomplete="off" required></label>'
    + '<p class="muted">A token is handed out once, when a member is added. v1 has no password and no magic link yet.</p>'
    + '<button class="btn" type="submit">Sign in</button></form></div>'
    + '<div class="card pad" data-panel="new" hidden>'
    + '<form class="stack" data-form="new">'
    + '<label>Household name<input type="text" name="name" placeholder="The Iyer family" required maxlength="100"></label>'
    + '<label>Your name<input type="text" name="display_name" placeholder="Amma" required maxlength="100"></label>'
    + '<label>Your email<input type="email" name="email" placeholder="you@example.com" required></label>'
    + '<p class="muted">Creates the household and makes you its first guardian. The token comes back once &mdash; keep it.</p>'
    + '<button class="btn" type="submit">Create household</button></form></div>'
    + '</div>';

  root.querySelectorAll('[data-tab]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const want = btn.dataset.tab;
      root.querySelectorAll('[data-tab]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.tab === want)));
      root.querySelectorAll('[data-panel]').forEach((p) => { p.hidden = p.dataset.panel !== want; });
    });
  });

  root.querySelector('[data-form="in"]').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const token = new FormData(ev.target).get('token').trim();
    if (!token) return;
    store.token = token;
    try {
      await boot();
    } catch (err) {
      store.token = null;
      flash('That token was not accepted.', true);
    }
  });

  root.querySelector('[data-form="new"]').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const button = ev.target.querySelector('button');
    button.disabled = true;
    try {
      const made = await api('/households', {
        method: 'POST',
        json: {
          name: f.get('name'),
          guardian: { display_name: f.get('display_name'), email: f.get('email') },
        },
      });
      const token = made.credentials.token;
      store.token = token;
      writeToken(token);
      root.querySelector('[data-panel="new"]').innerHTML =
        '<div class="stack"><h3>Keep this token</h3>'
        + '<p class="muted">It is shown once. Anyone holding it is signed in as you.</p>'
        + '<div class="token-out">' + esc(token) + '</div>'
        + '<button class="btn" data-continue>I have saved it &mdash; continue</button></div>';
      root.querySelector('[data-continue]').addEventListener('click', () => { boot().catch(fail); });
    } catch (err) {
      button.disabled = false;
      fail(err);
    }
  });
}

async function boot() {
  const me = await api('/me');
  store.me = me;
  store.token = store.token;
  writeToken(store.token);
  store.household = await api('/household');
  if (me.role === 'guardian') {
    try {
      const q = await api('/quarantine');
      store.quarantineCount = q.length;
    } catch (e) { store.quarantineCount = 0; }
  }
  try {
    store.amendmentCount = (await api('/amendments?status=proposed')).length;
  } catch (e) { store.amendmentCount = 0; }
  await render();
}

/* ── notices ──────────────────────────────────────────────────── */

const CHANNEL = {
  upload: { glyph: '↑', tone: '' },
  email: { glyph: '✉', tone: 'blue' },
  email_attachment: { glyph: '▣', tone: 'amber' },
};

async function noticesView(main) {
  const [items, mailboxes] = await Promise.all([
    api('/artifacts?limit=100'),
    // Only so Ways in can say whether a mailbox is connected.
    api('/google/mailboxes').catch(() => []),
  ]);
  store.mailboxes = mailboxes;
  main.innerHTML = '<div class="head"><div><h1>Notices</h1>'
    + '<p>Everything that arrived, kept exactly as it came. You see what is shared with the household, and what you sent yourself.</p></div>'
    + '<div class="row" style="gap:8px"><button class="btn ghost" data-reportgap>Something missing?</button>'
    + '<button class="btn" data-add>Add a notice</button></div></div>'
    + inboundCard()
    + '<div style="height:14px"></div>'
    + '<div class="card" data-list></div>'
    + uploadDialog()
    + gapDialog();

  const list = main.querySelector('[data-list]');
  if (!items.length) {
    list.innerHTML = '<div class="empty">Nothing yet. Upload a notice, or forward one to the address above.</div>';
  } else {
    list.className = 'card list';
    list.innerHTML = items.map(noticeRow).join('');
  }

  const dlg = main.querySelector('dialog');
  wireUpload(dlg);
  wireWays(main, dlg);
  wireGaps(main);
}

function wireWays(main, dlg) {
  main.querySelectorAll('[data-copy]').forEach((b) => b.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(b.dataset.copy);
      flash('Copied.');
    } catch (e) { flash('Could not reach the clipboard.', true); }
  }));
  // Every one of them: there is a button in the head and another inside the
  // Ways in card, and querySelector only ever found the first. On a page with
  // no upload dialog it goes to the page that has one.
  main.querySelectorAll('[data-add]').forEach((add) => add.addEventListener(
    'click', () => (dlg ? dlg.showModal() : go('/notices'))));
  const how = main.querySelector('[data-shortcut]');
  if (how) how.addEventListener('click', () => shortcutHelp(main));
}

function shortcutHelp(main) {
  const existing = main.querySelector('dialog[data-help]');
  if (existing) { existing.showModal(); return; }
  const host = location.origin;
  const el = document.createElement('dialog');
  el.setAttribute('data-help', '');
  el.className = 'card';
  el.style.cssText = 'max-width:480px;padding:20px;border:1px solid var(--line)';
  el.innerHTML = '<div class="stack">'
    + '<h2>Share straight from your phone</h2>'
    + '<p class="muted" style="line-height:1.55"><b>iPhone.</b> Shortcuts \u2192 new shortcut \u2192 \u24D8 \u2192 '
    + 'turn on <b>Show in Share Sheet</b>. Add one action, <b>Get Contents of URL</b>:</p>'
    + '<pre class="recipe">URL      ' + esc(host) + '/v1/artifacts\n'
    + 'Method   POST\n'
    + 'Headers  Authorization : Bearer &lt;your token&gt;\n'
    + 'Body     Form\n'
    + '           file = Shortcut Input</pre>'
    + '<p class="muted" style="line-height:1.55">It then appears in WhatsApp\u2019s share sheet. The token sits in '
    + 'the shortcut in plain text, so if the phone is lost use <b>Sign out everywhere</b> on the Household page.</p>'
    + '<p class="muted" style="line-height:1.55"><b>Android.</b> The same request from Tasker or MacroDroid, which '
    + 'can also fire on a notification \u2014 the only way to catch something nobody opened. iPhones cannot do that.</p>'
    + '<div class="row" style="justify-content:flex-end"><button class="btn" data-close-help>Close</button></div>'
    + '</div>';
  main.appendChild(el);
  el.querySelector('[data-close-help]').addEventListener('click', () => el.close());
  el.showModal();
}

function inboundCard() {
  const h = store.household || {};
  const ways = [
    {
      key: 'paste',
      live: true,
      glyph: '\u270E',
      tone: '',
      title: 'Paste or upload',
      what: 'Copy a WhatsApp message and paste it, or add a photo, screenshot or PDF.',
      value: '',
      hint: 'Copied text reads better than a screenshot of it.',
    },
    {
      key: 'whatsapp',
      live: !!h.whatsapp_number,
      glyph: '\u2709',
      tone: 'green',
      title: 'Forward on WhatsApp',
      what: 'Long-press the message, Forward, and pick this number. Several at once is fine.',
      value: h.whatsapp_number || '',
      hint: h.whatsapp_number
        ? 'Only numbers saved against a member are accepted.'
        : 'Not set up yet \u2014 needs a WhatsApp business number. Paste works meanwhile.',
    },
    {
      key: 'email',
      live: !!h.inbound_address,
      glyph: '\u2192',
      tone: 'blue',
      title: 'Forward by email',
      what: 'Give this address to the school, or forward circulars to it yourself.',
      value: h.inbound_address || '',
      hint: 'Mail from a member whose provider vouched for it is accepted; anything else waits in quarantine.',
    },
    {
      key: 'gmail',
      live: ((store.mailboxes || []).some((m) => !m.needs_reconnect)),
      glyph: '\u25D4',
      tone: 'blue',
      title: 'Connect your Gmail',
      what: 'Read-only, and only mail that looks like a school notice. Nothing is sent or changed.',
      value: '',
      hint: ((store.mailboxes || []).some((m) => m.needs_reconnect))
        ? 'A connected mailbox has stopped being readable \u2014 reconnect it on the Household page.'
        : 'Removes the step where somebody has to notice a notice and forward it.',
    },
    {
      key: 'phone',
      live: true,
      glyph: '\u2191',
      tone: 'amber',
      title: 'From your phone',
      what: 'Share straight from WhatsApp or the school app, without opening this.',
      value: '',
      hint: 'iPhone: a Shortcut in the share sheet. Android: Tasker can also catch notifications nobody opened.',
    },
  ];

  return '<section class="card pad stack" style="gap:12px">'
    + '<div class="row wrap" style="gap:8px"><h2>Ways in</h2>'
    + '<span class="muted">school notices arrive however they arrive</span></div>'
    + '<div class="ways">'
    + ways.map((w) =>
      '<div class="way' + (w.live ? '' : ' off') + '">'
      + '<div class="row" style="gap:9px;align-items:flex-start">'
      + '<span class="glyph ' + (w.live ? w.tone : '') + '" style="width:28px;height:28px;font-size:13px" aria-hidden="true">'
      + w.glyph + '</span>'
      + '<span class="grow"><span class="row wrap" style="gap:6px">'
      + '<span style="font-size:13px;font-weight:600">' + esc(w.title) + '</span>'
      + (w.live ? '' : '<span class="tag">not set up</span>') + '</span>'
      + '<span class="sub">' + esc(w.what) + '</span></span></div>'
      + (w.value
        ? '<div class="row" style="gap:7px"><code class="addr">' + esc(w.value) + '</code>'
          + '<button class="btn ghost sm" data-copy="' + esc(w.value) + '">Copy</button></div>'
        : '')
      + (w.key === 'paste'
        ? '<button class="btn sm" data-add style="align-self:flex-start">Add a notice</button>' : '')
      + (w.key === 'phone'
        ? '<button class="btn ghost sm" data-shortcut style="align-self:flex-start">How</button>' : '')
      + (w.key === 'gmail'
        ? '<a class="btn ghost sm" href="#/household" style="align-self:flex-start">Set up</a>' : '')
      + '<p class="muted" style="line-height:1.45">' + esc(w.hint) + '</p>'
      + '</div>').join('')
    + '</div></section>';
}

function noticeRow(a) {
  const ch = CHANNEL[a.channel] || CHANNEL.upload;
  const subjects = (a.subject_member_ids || []).map(memberName).filter(Boolean);
  return '<a class="item" href="#/notices/' + esc(a.id) + '">'
    + '<span class="glyph ' + ch.tone + '" aria-hidden="true">' + ch.glyph + '</span>'
    + '<span class="grow"><span class="row wrap" style="gap:8px">'
    + '<span class="title">' + esc(a.filename || a.source.subject || a.channel) + '</span>'
    + '<span class="tag ' + (a.visibility === 'shared' ? 'line' : 'solid') + '">' + esc(a.visibility) + '</span>'
    + subjects.map((n) => '<span class="tag kid">' + esc(n) + '</span>').join('')
    + '</span>'
    + '<span class="sub">' + esc(noticeSub(a)) + '</span></span>'
    + '<span class="muted" style="flex-shrink:0">' + esc(bytes(a.size_bytes)) + '</span>'
    + '<span class="muted" style="width:82px;text-align:right;flex-shrink:0">' + esc(ago(a.received_at)) + '</span>'
    + '</a>';
}

function noticeSub(a) {
  const bits = [];
  if (a.source && a.source.from) bits.push('from ' + a.source.from);
  if (a.submitted_by) bits.push('by ' + (memberName(a.submitted_by) || 'a member'));
  bits.push(a.media_type);
  if (a.children && a.children.length) bits.push(a.children.length + ' attachment' + (a.children.length > 1 ? 's' : ''));
  return bits.join('  ·  ');
}

function uploadDialog() {
  const kids = membersByRole(['child', 'adult', 'guardian']);
  return '<dialog class="card" style="max-width:440px;padding:20px;border:1px solid var(--line)">'
    + '<form class="stack" data-form="upload">'
    + '<h2>Add a notice</h2>'
    + '<div class="tabs"><button type="button" data-how="paste" aria-pressed="true">Paste a message</button>'
    + '<button type="button" data-how="file" aria-pressed="false">Upload a file</button></div>'
    + '<label data-pane="paste">Paste it here'
    + '<textarea name="text" rows="7" placeholder="Long-press the WhatsApp message, Copy, then paste here."></textarea>'
    + '</label>'
    + '<p class="muted" data-pane="paste">A screenshot works too, but copied text reads better than OCR of it.</p>'
    + '<label data-pane="file" hidden>File<input type="file" name="file"></label>'
    + '<label>Who can see it<select name="visibility">'
    + '<option value="private">Private to me</option><option value="shared">Shared with the household</option>'
    + '</select></label>'
    + '<label>Who it is about<select name="subject" >'
    + '<option value="">Nobody in particular</option>'
    + kids.map((m) => '<option value="' + esc(m.id) + '">' + esc(m.display_name) + ' (' + esc(m.role) + ')</option>').join('')
    + '</select></label>'
    + '<p class="muted">Naming a child needs an active consent record for them.</p>'
    + '<div class="row" style="justify-content:flex-end"><button class="btn ghost" type="button" data-close>Cancel</button>'
    + '<button class="btn" type="submit">Upload</button></div>'
    + '</form></dialog>';
}

function wireUpload(dlg) {
  dlg.querySelector('[data-close]').addEventListener('click', () => dlg.close());

  let how = 'paste';
  dlg.querySelectorAll('[data-how]').forEach((tab) => {
    tab.addEventListener('click', () => {
      how = tab.dataset.how;
      dlg.querySelectorAll('[data-how]').forEach((t) => t.setAttribute('aria-pressed', String(t === tab)));
      dlg.querySelectorAll('[data-pane]').forEach((p) => { p.hidden = p.dataset.pane !== how; });
    });
  });

  dlg.querySelector('form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const form = ev.target;
    const f = new FormData(form);

    let file;
    if (how === 'paste') {
      const text = (f.get('text') || '').trim();
      if (!text) { flash('Paste the message first.', true); return; }
      // A pasted message is a notice like any other: same bytes-in, same
      // hash, same dedup. Two parents pasting the same message store once.
      const stamp = new Date().toISOString().slice(0, 16).replace('T', ' ');
      file = new File([text], 'pasted ' + stamp + '.txt', { type: 'text/plain' });
    } else {
      file = f.get('file');
      if (!file || !file.size) { flash('Pick a file first.', true); return; }
    }

    const body = new FormData();
    body.append('file', file);
    body.append('visibility', f.get('visibility'));
    if (f.get('subject')) body.append('subject_member_ids', f.get('subject'));
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    try {
      const out = await api('/artifacts', { method: 'POST', body });
      dlg.close();
      flash(out.duplicate ? 'You had already sent those exact bytes — showing the first one.' : 'Stored. Reading it now.');
      go('/notices/' + out.artifact.id);
      await render();
    } catch (err) {
      fail(err);
    } finally {
      button.disabled = false;
    }
  });
}

/* ── one notice: the review card ──────────────────────────────── */

async function noticeView(main, args) {
  const id = args[0];
  const artifact = await api('/artifacts/' + id);
  let extraction = null;
  try {
    extraction = await api('/artifacts/' + id + '/extraction');
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 404) throw err;
  }

  main.innerHTML = noticeHeader(artifact)
    + '<div class="split">'
    + '<div class="viewer" data-viewer></div>'
    + '<div class="stack" data-side></div>'
    + '</div>';

  renderSide(main.querySelector('[data-side]'), artifact, extraction, id);
  wireNoticeHeader(main, artifact, id);
  renderOriginal(main.querySelector('[data-viewer]'), artifact).catch((err) => {
    main.querySelector('[data-viewer]').innerHTML =
      '<header><b>Original</b></header><div class="stage"><p class="note">Could not load the original: '
      + esc(err.message) + '</p></div>';
  });
}

function noticeHeader(a) {
  const auth = a.source && a.source.sender_authenticated;
  const claimsFrom = a.source && a.source.from;
  return '<div class="head"><div>'
    + '<p class="muted" style="margin-bottom:6px"><a href="#/notices" style="color:var(--ink-3);text-decoration:none">Notices</a> / '
    + esc(a.channel.replace('_', ' ')) + '</p>'
    + '<h1>' + esc(a.filename || (a.source && a.source.subject) || 'Notice') + '</h1>'
    + '<p class="row wrap" style="gap:8px;margin-top:9px">'
    + '<span class="tag ' + ((CHANNEL[a.channel] || {}).tone || '') + '">' + esc(a.channel.replace('_', ' ')) + '</span>'
    + '<span class="tag ' + (a.visibility === 'shared' ? 'line' : 'solid') + '">' + esc(a.visibility) + '</span>'
    + (a.status === 'quarantined' ? '<span class="tag rust">quarantined</span>' : '')
    + (claimsFrom ? '<span class="muted">from <b style="color:var(--ink);font-weight:500">' + esc(claimsFrom) + '</b></span>' : '')
    + (a.submitted_by ? '<span class="muted">' + esc(memberName(a.submitted_by)) + '</span>' : '')
    + '<span class="muted">' + esc(date(a.received_at)) + '</span>'
    + (a.source && a.source.from && a.channel !== 'whatsapp' && a.channel !== 'whatsapp_media'
        ? '<span class="tag ' + (auth ? 'green' : 'red') + '">' + (auth ? 'sender authenticated' : 'sender not authenticated') + '</span>'
        : '')
    + (a.source && a.source.forwarded
        ? '<span class="tag amber" title="Relayed by someone, not issued by the school">second-hand</span>'
        : '')
    + '</p></div>'
    + '<div class="row">'
    + (a.submitted_by === (store.me || {}).id && !a.parent_id
        ? '<button class="btn ghost" data-vis>Make ' + (a.visibility === 'shared' ? 'private' : 'shared') + '</button>' : '')
    + '<button class="btn ghost" data-reread>Read again</button>'
    + '<button class="btn ghost" data-download>Download original</button>'
    + '</div></div>';
}

function wireNoticeHeader(main, artifact, id) {
  const vis = main.querySelector('[data-vis]');
  if (vis) {
    vis.addEventListener('click', async () => {
      try {
        await api('/artifacts/' + id, { method: 'PATCH', json: { visibility: artifact.visibility === 'shared' ? 'private' : 'shared' } });
        flash('Visibility changed.');
        await render();
      } catch (err) { fail(err); }
    });
  }
  main.querySelector('[data-reread]').addEventListener('click', async (ev) => {
    ev.target.disabled = true;
    try {
      await api('/artifacts/' + id + '/extract', { method: 'POST' });
      flash('Queued. It will re-read in the background.');
      setTimeout(() => { render().catch(() => {}); }, 1600);
    } catch (err) { fail(err); ev.target.disabled = false; }
  });
  main.querySelector('[data-download]').addEventListener('click', async () => {
    try {
      const blob = await api('/artifacts/' + id + '/original', { blob: true });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = artifact.filename || (id + '.bin');
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
    } catch (err) { fail(err); }
  });
}

/* the claims panel */

const KIND_TONE = {
  event: 'blue', deadline: 'amber', payment: 'green',
  form: 'amber', amendment: 'rust', instruction: '', info: '',
};

function renderSide(side, artifact, extraction, id) {
  const x = extraction && extraction.extraction;
  const claims = x ? x.claims : [];
  const grounded = claims.filter((c) => c.location && c.location.match);

  let metaCard = '';
  if (x) {
    metaCard = '<div class="card pad stack" style="gap:8px">'
      + '<div class="row wrap" style="gap:8px"><h2>Read from this notice</h2>'
      + '<span class="muted">' + claims.length + ' claim' + (claims.length === 1 ? '' : 's')
      + ' · ' + grounded.length + ' grounded</span></div>'
      + '<div class="row wrap" style="gap:6px">'
      + '<span class="tag line mono">' + esc(x.model || x.extractor) + '</span>'
      + '<span class="tag line mono">' + esc(x.prompt_version) + '</span>'
      + '<span class="muted">' + esc(x.page_count) + ' page' + (x.page_count === 1 ? '' : 's')
      + (x.ocr_pages ? ' · ' + x.ocr_pages + ' by OCR' : '') + '</span>'
      + (x.actionable ? '' : '<span class="tag">asks nothing of you</span>')
      + '</div>'
      + (x.non_actionable_reason ? '<p class="muted">' + esc(x.non_actionable_reason) + '</p>' : '')
      + (artifact.source && artifact.source.forwarded
        ? '<p class="muted" style="padding:9px 11px;background:var(--amber-tint);border-radius:6px;line-height:1.5">'
          + 'Forwarded, so this is somebody\u2019s retelling rather than the school\u2019s own notice. '
          + 'These claims are held a little less confidently until the original turns up.</p>'
        : '')
      + '</div>';
  } else {
    const status = extraction ? extraction.job_status : null;
    const err = extraction ? extraction.job_error : null;
    metaCard = '<div class="card pad stack" style="gap:8px"><h2>Read from this notice</h2>'
      + '<p class="muted">' + esc(
        err ? 'The reader failed: ' + err
          : status ? 'The reader is ' + status + '.'
            : artifact.status === 'quarantined'
              ? 'Quarantined mail is not read until a guardian accepts it.'
              : 'Nothing has been read from this yet.'
      ) + '</p></div>';
  }

  side.innerHTML = metaCard
    + (claims.length ? '<div class="stack" data-claims style="gap:9px"></div>' : '')
    + '<div class="card pad stack" data-obligations style="gap:10px"></div>';

  if (claims.length) {
    const box = side.querySelector('[data-claims]');
    box.innerHTML = claims.map((c, i) => claimCard(c, i)).join('');
    box.querySelectorAll('.claim').forEach((btn) => {
      btn.addEventListener('click', () => selectClaim(Number(btn.dataset.i), claims));
    });
    selectClaim(0, claims);
  }

  renderObligations(side.querySelector('[data-obligations]'), id);
}

function claimCard(c, i) {
  const loc = c.location || {};
  const placed = !!loc.match;
  const needs = (c.requires || []).map((r) => '<span class="need">' + esc(r.replace(/_/g, ' ')) + '</span>').join('');
  const when = c.date
    ? date(c.date) + (c.end_date && c.end_date !== c.date ? ' – ' + date(c.end_date) : '')
    : (c.date_text || '');
  const amount = money(c.amount, c.currency);
  return '<button class="claim' + (placed ? '' : ' ungrounded') + '" type="button" data-i="' + i
    + '" aria-pressed="false" aria-label="Claim ' + (i + 1) + ': ' + esc(c.title) + '">'
    + '<span class="row" style="align-items:flex-start;gap:10px">'
    + '<span class="n" aria-hidden="true">' + (i + 1) + '</span>'
    + '<span class="grow stack" style="gap:6px">'
    + '<span class="row wrap" style="gap:7px">'
    + '<span class="tag ' + (KIND_TONE[c.kind] || '') + '">' + esc(c.kind) + '</span>'
    + '<span style="font-size:14px;font-weight:600">' + esc(c.title) + '</span></span>'
    + '<span class="row wrap" style="gap:9px;font-size:12px;color:var(--ink-2)">'
    + (when ? '<span style="font-weight:500;color:var(--ink)">' + esc(when) + (c.uncertain ? ' (uncertain)' : '') + '</span>' : '')
    + (amount ? '<span style="font-weight:600;color:var(--ink)">' + esc(amount) + '</span>' : '')
    + (c.applies_to ? '<span class="tag line" style="text-transform:none;letter-spacing:0">' + esc(c.applies_to) + '</span>' : '')
    + (c.subject_name ? '<span class="tag kid">' + esc(c.subject_name) + '</span>' : '')
    + (c.optional ? '<span class="tag line" style="text-transform:none;letter-spacing:0">optional</span>' : '')
    + '</span>'
    + (needs ? '<span class="needs">' + needs + '</span>' : '')
    + '<span class="quote">&ldquo;' + esc(c.quote) + '&rdquo;</span>'
    + '<span class="foot">'
    + '<span class="tag ' + (loc.match === 'exact' ? 'green' : loc.match ? '' : 'red') + '">'
    + esc(loc.match || 'not found in the text') + '</span>'
    + (placed ? '<span>page ' + esc(loc.page) + ' · chars ' + esc(loc.span_start) + '–' + esc(loc.span_end) + '</span>' : '')
    + '<span class="grow"></span><span class="mono">' + Number(c.confidence).toFixed(2) + '</span>'
    + '</span></span></span></button>';
}

let viewerState = { pages: [], boxesFor: null };

function selectClaim(index, claims) {
  root.querySelectorAll('.claim').forEach((b) => {
    b.setAttribute('aria-pressed', String(Number(b.dataset.i) === index));
  });
  drawBoxes(claims[index]);
}

function drawBoxes(claim) {
  root.querySelectorAll('.box').forEach((b) => b.remove());
  const loc = claim && claim.location;
  if (!loc || !loc.match || !loc.boxes || !loc.boxes.length) return;
  const wrap = viewerState.pages[loc.page];
  if (!wrap) return;
  const scale = Number(wrap.dataset.scale) || 1;
  loc.boxes.forEach((b) => {
    const el = document.createElement('div');
    el.className = 'box';
    el.style.left = (b[0] * scale) + 'px';
    el.style.top = (b[1] * scale) + 'px';
    el.style.width = Math.max(2, (b[2] - b[0]) * scale) + 'px';
    el.style.height = Math.max(2, (b[3] - b[1]) * scale) + 'px';
    wrap.appendChild(el);
  });
  wrap.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

/* the original, rendered so boxes can sit on it */

async function renderOriginal(viewer, artifact) {
  viewerState = { pages: [], boxesFor: null };
  viewer.innerHTML = '<header><b>Original</b>'
    + '<span>' + esc(artifact.media_type) + ' · ' + esc(bytes(artifact.size_bytes)) + '</span>'
    + '<span class="grow"></span>'
    + '<span class="mono">sha256 ' + esc(String(artifact.sha256).slice(0, 10)) + '…</span></header>'
    + '<div class="stage"><p class="note">Fetching&hellip;</p></div>';
  const stage = viewer.querySelector('.stage');
  const blob = await api('/artifacts/' + artifact.id + '/original', { blob: true });

  if (artifact.media_type.startsWith('image/')) {
    const url = URL.createObjectURL(blob);
    stage.innerHTML = '';
    const wrap = document.createElement('div');
    wrap.className = 'page-wrap';
    const img = document.createElement('img');
    img.alt = 'The original notice';
    img.src = url;
    wrap.appendChild(img);
    stage.appendChild(wrap);
    await new Promise((res) => { img.onload = res; img.onerror = res; });
    // Boxes for an image are in its own pixels, so the factor is how much the
    // browser had to shrink it to fit.
    wrap.dataset.scale = String(img.naturalWidth ? img.clientWidth / img.naturalWidth : 1);
    viewerState.pages[1] = wrap;
    return;
  }

  if (artifact.media_type === 'application/pdf') {
    try {
      await renderPdf(stage, await blob.arrayBuffer());
      return;
    } catch (err) {
      stage.innerHTML = '<p class="note">The page renderer could not load, so the words cannot be '
        + 'highlighted here. Each claim still shows the text it was read from.</p>';
      const obj = document.createElement('object');
      obj.type = 'application/pdf';
      obj.data = URL.createObjectURL(blob);
      stage.appendChild(obj);
      return;
    }
  }

  if (artifact.media_type.startsWith('text/') || artifact.media_type === 'message/rfc822') {
    const text = await blob.text();
    stage.innerHTML = '<pre style="margin:0;width:100%;max-height:620px;overflow:auto;background:#fff;'
      + 'padding:18px;border-radius:6px;font:400 12px/1.6 var(--mono);white-space:pre-wrap">'
      + esc(text.slice(0, 200000)) + '</pre>';
    return;
  }

  stage.innerHTML = '<p class="note">' + esc(artifact.media_type)
    + ' cannot be shown here. Use &ldquo;Download original&rdquo; — every read is audited either way.</p>';
}

let pdfjsLoading = null;
function loadPdfjs() {
  if (window.pdfjsLib) return Promise.resolve(window.pdfjsLib);
  if (pdfjsLoading) return pdfjsLoading;
  pdfjsLoading = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = PDFJS_BASE + '/pdf.min.js';
    s.onload = () => {
      if (!window.pdfjsLib) { reject(new Error('pdf.js did not register')); return; }
      window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_BASE + '/pdf.worker.min.js';
      resolve(window.pdfjsLib);
    };
    s.onerror = () => reject(new Error('pdf.js could not be fetched'));
    document.head.appendChild(s);
  });
  return pdfjsLoading;
}

async function renderPdf(stage, buffer) {
  const lib = await loadPdfjs();
  const pdf = await lib.getDocument({ data: buffer }).promise;
  stage.innerHTML = '';
  const width = Math.min(stage.clientWidth - 40, 900);
  for (let n = 1; n <= pdf.numPages; n += 1) {
    const page = await pdf.getPage(n);
    const base = page.getViewport({ scale: 1 });
    // PyMuPDF gives box coordinates in PDF points from the top left, which is
    // the same origin the canvas uses, so one factor maps both.
    const scale = width / base.width;
    const viewport = page.getViewport({ scale });
    const wrap = document.createElement('div');
    wrap.className = 'page-wrap';
    wrap.dataset.scale = String(scale);
    const canvas = document.createElement('canvas');
    canvas.width = Math.floor(viewport.width);
    canvas.height = Math.floor(viewport.height);
    canvas.style.width = Math.floor(viewport.width) + 'px';
    canvas.style.height = Math.floor(viewport.height) + 'px';
    wrap.appendChild(canvas);
    stage.appendChild(wrap);
    viewerState.pages[n] = wrap;
    await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
  }
  const selected = root.querySelector('.claim[aria-pressed="true"]');
  if (selected) selected.click();
}

/* ── obligations ──────────────────────────────────────────────── */

const ACTION = {
  sign: { glyph: '✎', tone: 'amber' },
  pay: { glyph: '₹', tone: 'green' },
  attend: { glyph: '⚑', tone: 'blue' },
  submit: { glyph: '↗', tone: 'amber' },
  prepare: { glyph: '▣', tone: '' },
  note: { glyph: '○', tone: '' },
};

async function renderObligations(box, artifactId) {
  const all = await api('/obligations?status=&limit=200');
  const mine = artifactId ? all.filter((o) => o.artifact_id === artifactId) : all;
  box.innerHTML = '<div class="row wrap" style="gap:8px"><h2>Proposed for the family</h2>'
    + '<span class="muted">nothing is accepted until someone says so</span></div>'
    + (mine.length ? '<div class="stack" style="gap:7px" data-obs></div>'
      : '<p class="muted">Nothing proposed from this notice.</p>');
  if (mine.length) wireObligationRows(box.querySelector('[data-obs]'), mine);
}

function obligationRow(o, withLink) {
  const act = ACTION[o.action] || ACTION.note;
  const done = o.status !== 'proposed';
  const gone = !!o.superseded_at;
  const bits = [o.kind, o.due_date ? 'due ' + date(o.due_date) : null, o.due_time,
    o.subject_member_id ? 'for ' + memberName(o.subject_member_id) : null,
    o.action === 'pay' ? 'reminder only — nothing here pays' : null].filter(Boolean);
  return '<div class="row card" style="gap:12px;padding:12px 14px;background:'
    + (done ? 'var(--sunk)' : 'var(--card)') + ';opacity:' + (o.status === 'dismissed' ? '0.5' : '1') + '">'
    + '<span class="glyph ' + (done ? 'green' : act.tone) + '" aria-hidden="true">'
    + (o.status === 'accepted' ? '✓' : act.glyph) + '</span>'
    + '<span class="grow"><span class="row wrap" style="gap:7px">'
    + '<span style="font-size:13.5px;font-weight:600' + (gone ? ';text-decoration:line-through' : '') + '">'
    + esc(o.title) + '</span>'
    + (gone ? '<a class="tag amber" href="#/notices/' + esc(o.superseded_by_artifact_id)
      + '" style="text-decoration:none">superseded \u2014 see the notice this came from</a>' : '')
    + (o.optional ? '<span class="tag line" style="text-transform:none;letter-spacing:0">optional</span>' : '')
    + (done ? '<span class="tag ' + (o.status === 'accepted' ? 'green' : '') + '">' + esc(o.status) + '</span>' : '')
    + '</span><span class="sub">' + esc(bits.join('  ·  '))
    + (withLink ? '  ·  <a href="#/notices/' + esc(o.artifact_id) + '">see the words this came from</a>' : '')
    + '</span></span>'
    + (o.status === 'proposed'
      ? '<button class="btn sm" data-accept="' + esc(o.id) + '">Accept</button>'
        + '<button class="icon-btn" data-dismiss="' + esc(o.id) + '" aria-label="Dismiss">×</button>'
      : '<button class="btn ghost sm" data-accept="' + esc(o.id) + '" '
        + (o.status === 'accepted' ? 'disabled' : '') + '>Accept</button>')
    + '</div>';
}

function wireObligationRows(container, rows, withLink) {
  container.innerHTML = rows.map((o) => obligationRow(o, withLink)).join('');
  const decide = async (id, status) => {
    try {
      await api('/obligations/' + id + '/decision', { method: 'POST', json: { status } });
      flash(status === 'accepted' ? 'Accepted.' : 'Dismissed.');
      await render();
    } catch (err) { fail(err); }
  };
  container.querySelectorAll('[data-accept]').forEach((b) =>
    b.addEventListener('click', () => decide(b.dataset.accept, 'accepted')));
  container.querySelectorAll('[data-dismiss]').forEach((b) =>
    b.addEventListener('click', () => decide(b.dataset.dismiss, 'dismissed')));
}

/* ── the brief ────────────────────────────────────────────────── */
/* Worst first, and never more than a few lines: a brief that lists everything
   is the list it was meant to replace. The server decides what matters and in
   what order; this only draws it. */

const REASON = {
  missed: { tone: 'red', label: 'missed', glyph: '!' },
  mailbox: { tone: 'red', label: 'not being read', glyph: '\u2709' },
  overdue: { tone: 'red', label: 'overdue', glyph: '!' },
  today: { tone: 'amber', label: 'today', glyph: '\u25CF' },
  undecided: { tone: 'amber', label: 'undecided', glyph: '?' },
  soon: { tone: 'blue', label: 'soon', glyph: '\u2192' },
  amendment: { tone: 'rust', label: 'may have changed', glyph: '\u21BB' },
  quarantine: { tone: 'rust', label: 'needs vouching', glyph: '\u29B8' },
};

function greeting() {
  const hour = new Date().getHours();
  if (hour < 12) return 'Good morning';
  return hour < 18 ? 'Good afternoon' : 'Good evening';
}

/* Each line goes where the thing is actually dealt with. */
function briefHref(i) {
  if (i.reason === 'mailbox') return '#/household';
  if (i.reason === 'amendment') return '#/revisions';
  if (i.reason === 'quarantine') return '#/quarantine';
  return i.artifact_id ? '#/notices/' + i.artifact_id : '#/today';
}

function briefRow(i) {
  const r = REASON[i.reason] || { tone: '', label: i.reason, glyph: '\u00B7' };
  return '<a class="brief-row row" href="' + briefHref(i) + '" style="gap:10px;align-items:flex-start">'
    + '<span class="glyph ' + r.tone + '" style="width:26px;height:26px;font-size:12px;flex-shrink:0"'
    + ' aria-hidden="true">' + r.glyph + '</span>'
    + '<span class="grow"><span class="row wrap" style="gap:6px">'
    + '<span style="font-size:13.5px;font-weight:600">' + esc(i.title) + '</span>'
    + '<span class="tag ' + r.tone + '">' + esc(r.label) + '</span>'
    + (i.optional ? '<span class="tag line">optional</span>' : '')
    + (i.subject_name ? '<span class="tag kid">' + esc(i.subject_name) + '</span>' : '')
    // A pending revision means the date shown may not be the date that holds.
    + (i.contested && i.reason !== 'amendment'
      ? '<span class="tag rust" title="A later notice looks like it changes this">may have changed</span>' : '')
    + '</span>'
    + '<span class="sub">' + esc(i.why) + (i.when ? ' \u00B7 ' + esc(i.when) : '') + '</span></span></a>';
}

function briefCard(b) {
  return '<section class="card pad stack brief" style="gap:13px">'
    + '<div class="row wrap" style="gap:10px">'
    + '<h1 style="margin:0">' + esc(greeting()) + ', ' + esc(b.display_name) + '</h1>'
    + '<span class="rule"></span>'
    + '<span class="muted" style="flex-shrink:0">' + esc(date(b.date)) + '</span></div>'
    + (b.items.length
      ? '<div class="stack" style="gap:9px">' + b.items.map(briefRow).join('') + '</div>'
        + (b.more ? '<span class="muted">and ' + b.more + ' more below</span>' : '')
      : '<p class="muted">Nothing needs anyone today. A dated thing appears here before its day, and anything '
        + 'nobody has decided appears sooner than that.</p>')
    + '</section>';
}

/* ── today ────────────────────────────────────────────────────── */

async function todayView(main) {
  const [brief, proposed, upcoming] = await Promise.all([
    api('/brief?limit=3'),
    api('/obligations?status=proposed&limit=200'),
    api('/reminders?limit=200'),
  ]);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const week = new Date(today); week.setDate(week.getDate() + 7);

  const bucket = { Overdue: [], 'This week': [], Later: [], 'No date': [] };
  proposed.forEach((o) => {
    if (!o.due_date) { bucket['No date'].push(o); return; }
    const d = new Date(o.due_date + 'T00:00:00');
    if (d < today) bucket.Overdue.push(o);
    else if (d <= week) bucket['This week'].push(o);
    else bucket.Later.push(o);
  });

  const TONE = { Overdue: 'red', 'This week': 'amber', Later: '', 'No date': '' };
  main.innerHTML = briefCard(brief)
    + '<div class="head"><div><h1>Waiting on you</h1>'
    + '<p>Proposed from notices you can see. Nothing here has been agreed to yet, and nothing here pays.</p></div>'
    + '<a class="btn ghost" href="#/notices">All notices</a></div>'
    + '<div class="split"><div>'
    + (proposed.length ? '<div class="stack" style="gap:20px" data-groups></div>'
      : '<div class="card"><div class="empty">Nothing is waiting on you. Anything a notice asks for shows up here first as a proposal.</div></div>')
    + '</div><aside class="stack" data-side></aside></div>';

  renderUpcoming(main.querySelector('[data-side]'), upcoming);
  if (!proposed.length) return;
  const groups = main.querySelector('[data-groups]');
  Object.keys(bucket).forEach((label) => {
    if (!bucket[label].length) return;
    const sec = document.createElement('section');
    sec.className = 'stack';
    sec.style.gap = '8px';
    sec.innerHTML = '<div class="row"><span class="tag ' + TONE[label] + '">' + esc(label)
      + '</span><span class="rule"></span></div><div data-rows class="stack" style="gap:7px"></div>';
    groups.appendChild(sec);
    wireObligationRows(sec.querySelector('[data-rows]'), bucket[label], true);
  });
}

function renderUpcoming(side, reminders) {
  const pending = reminders.filter((r) => r.status === 'pending');
  const sent = reminders.filter((r) => r.status === 'sent');
  side.innerHTML = '<div class="card pad stack" style="gap:11px">'
    + '<div class="row wrap" style="gap:8px"><h2>You will be told</h2>'
    + '<span class="muted">' + pending.length + ' to come</span></div>'
    + (pending.length
      ? '<div class="stack" style="gap:8px">' + pending.slice(0, 8).map(reminderRow).join('') + '</div>'
      : '<p class="muted">Nothing queued. A dated task is reminded about before its day.</p>')
    + (sent.length ? '<p class="muted" style="padding-top:4px;border-top:1px solid var(--line-soft)">'
      + sent.length + ' already sent.</p>' : '')
    + '</div>'
    + '<div class="card pad stack" style="gap:7px">'
    + '<span style="font-size:12.5px;font-weight:600">Who gets told</span>'
    + '<span class="muted" style="line-height:1.55">A shared notice reminds the adults and guardians. A private one '
    + 'reminds only the member who sent it. Children are never told: a task names a child because it is '
    + '<em>about</em> them.</span></div>';
}

function reminderRow(r) {
  const nudge = r.reason === 'undecided';
  return '<div class="row" style="gap:10px;align-items:flex-start">'
    + '<span class="lead" aria-hidden="true">' + (r.lead_days === 0 ? 'day' : r.lead_days + 'd') + '</span>'
    + '<span class="grow"><span style="font-size:12.5px;font-weight:500;display:block">' + esc(r.title) + '</span>'
    + '<span class="muted">' + (nudge ? 'nudge \u2014 still undecided' : 'due ' + esc(date(r.due_date)))
    + '  \u00B7  ' + esc(ago(r.send_after).replace(' ago', ' from now').replace('just now', 'now')) + '</span></span>'
    + '</div>';
}

/* ── quarantine ───────────────────────────────────────────────── */

async function quarantineView(main) {
  const items = await api('/quarantine');
  store.quarantineCount = items.length;
  main.innerHTML = '<div class="head"><div><div class="row" style="gap:10px"><h1>Quarantine</h1>'
    + '<span class="tag rust">guardians only</span></div>'
    + '<p style="max-width:760px">Mail whose sender we could not place, or whose provider would not vouch for the address it '
    + 'claims. It is kept, encrypted, and nothing has read it. Say who it belongs to and it joins the household; '
    + 'reject it and the bytes go.</p></div></div>'
    + (items.length ? '<div class="stack" data-items></div>'
      : '<div class="card"><div class="empty">Nothing is waiting. Mail from a member whose provider authenticated it is accepted straight away.</div></div>');
  if (!items.length) return;

  const box = main.querySelector('[data-items]');
  const people = membersByRole(['guardian', 'adult']);
  box.innerHTML = items.map((q) => {
    const src = q.source || {};
    return '<article class="card pad stack" data-q="' + esc(q.id) + '" style="gap:14px">'
      + '<div class="row wrap" style="gap:13px;align-items:flex-start">'
      + '<span class="glyph ' + (q.quarantine_reason === 'unknown_sender' ? 'rust' : 'amber') + '" aria-hidden="true">!</span>'
      + '<span class="grow"><span class="row wrap" style="gap:9px">'
      + '<span style="font-size:15px;font-weight:600">' + esc(src.subject || q.filename || 'No subject') + '</span>'
      + '<span class="tag ' + (q.quarantine_reason === 'unknown_sender' ? 'rust' : 'red') + '">'
      + esc(String(q.quarantine_reason || '').replace(/_/g, ' ')) + '</span></span>'
      + '<span class="sub">from <b class="mono" style="color:var(--ink);font-weight:500">' + esc(src.from || 'unknown')
      + '</b>  ·  ' + esc(date(q.received_at)) + '  ·  ' + esc(bytes(q.size_bytes))
      + '  ·  ' + (q.children || []).length + ' attachment' + ((q.children || []).length === 1 ? '' : 's')
      + '</span></span></div>'
      + '<div class="row wrap" style="gap:10px;padding:10px 13px;background:var(--sunk);border:1px solid var(--line-soft);border-radius:8px">'
      + '<span class="mono" style="font-size:11px;font-weight:500">sender authenticated</span>'
      + '<span class="mono" style="font-size:11px;color:var(--ink-2)">' + (src.sender_authenticated ? 'yes' : 'no')
      + '</span><span class="rule"></span>'
      + '<span class="mono" style="font-size:11px;color:var(--ink-2)">message-id ' + esc(src.message_id || '—') + '</span>'
      + '</div>'
      + '<div class="row wrap" style="gap:10px">'
      + '<span class="muted">If you know this sender, say whose it is:</span>'
      + '<span class="row wrap" style="gap:6px" data-owners>'
      + people.map((m) => '<button class="pill" data-owner="' + esc(m.id) + '" aria-pressed="false">'
        + esc(m.display_name) + '</button>').join('')
      + '</span><span class="grow"></span>'
      + '<select data-visibility style="width:auto"><option value="private">Private to them</option>'
      + '<option value="shared">Shared with the household</option></select>'
      + '<button class="btn ghost" data-reject>Reject and delete</button>'
      + '<button class="btn" data-accept disabled>Accept</button>'
      + '</div></article>';
  }).join('');

  box.querySelectorAll('[data-q]').forEach((card) => {
    const id = card.dataset.q;
    let owner = null;
    const accept = card.querySelector('[data-accept]');
    card.querySelectorAll('[data-owner]').forEach((b) => {
      b.addEventListener('click', () => {
        owner = b.dataset.owner;
        card.querySelectorAll('[data-owner]').forEach((o) => o.setAttribute('aria-pressed', String(o === b)));
        accept.disabled = false;
        accept.textContent = 'Accept for ' + memberName(owner);
      });
    });
    accept.addEventListener('click', async () => {
      if (!owner) return;
      accept.disabled = true;
      try {
        await api('/quarantine/' + id + '/accept', {
          method: 'POST',
          json: { member_id: owner, visibility: card.querySelector('[data-visibility]').value },
        });
        flash('Accepted. It will be read now.');
        await render();
      } catch (err) { fail(err); accept.disabled = false; }
    });
    card.querySelector('[data-reject]').addEventListener('click', async () => {
      if (!confirm('Reject this? The stored bytes are deleted and this cannot be undone.')) return;
      try {
        await api('/quarantine/' + id + '/reject', { method: 'POST' });
        flash('Rejected and deleted.');
        await render();
      } catch (err) { fail(err); }
    });
  });
}

/* -- revisions -------------------------------------------------- */

async function amendmentsView(main) {
  const proposed = await api('/amendments?status=proposed&limit=200');
  const all = await api('/amendments?status=&limit=200');
  const settled = all.filter((a) => a.status !== 'proposed');
  store.amendmentCount = proposed.length;

  main.innerHTML = '<div class="head"><div><h1>Revisions</h1>'
    + '<p style="max-width:780px">A notice that looks like it changes an earlier one. Matching two notices by their '
    + 'words is a guess, so nothing is applied until you say so. Until then <b>both still remind you</b>, and the '
    + 'reminder for the older one says a later notice may have changed it.</p></div></div>'
    + (proposed.length ? '<div class="stack" data-proposed></div>'
      : '<div class="card"><div class="empty">Nothing is waiting. When a notice revises an earlier one, the link to '
        + 'confirm shows up here.</div></div>')
    + (settled.length ? '<h2 style="margin:26px 0 12px">Already decided</h2><div class="card list" data-settled></div>' : '');

  if (proposed.length) {
    const box = main.querySelector('[data-proposed]');
    box.innerHTML = proposed.map(amendmentCard).join('');
    box.querySelectorAll('[data-decide]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const status = btn.dataset.decide;
        if (status === 'confirmed'
          && !confirm('Confirm that this replaces the earlier notice?\n\nThe earlier notice’s tasks are superseded '
            + 'and stop reminding. If the two are not actually the same thing, nobody will be reminded about the '
            + 'earlier one again.')) return;
        btn.disabled = true;
        try {
          const out = await api('/amendments/' + btn.dataset.id + '/decision', { method: 'POST', json: { status } });
          flash(status === 'confirmed'
            ? 'Linked. ' + out.obligations_superseded + ' task'
              + (out.obligations_superseded === 1 ? '' : 's') + ' superseded.'
            : 'Left as two separate notices.');
          await render();
        } catch (err) { fail(err); btn.disabled = false; }
      });
    });
  }

  if (settled.length) {
    main.querySelector('[data-settled]').innerHTML = settled.map((a) =>
      '<div class="item" style="cursor:default">'
      + '<span class="tag ' + (a.status === 'confirmed' ? 'green' : '') + '">' + esc(a.status) + '</span>'
      + '<span class="grow"><span class="title">' + esc(a.claim_title) + '</span>'
      + '<span class="sub">' + (a.status === 'confirmed' ? 'replaced' : 'kept separate from') + ' “'
      + esc(a.amends_title) + '”  ·  ' + esc(memberName(a.decided_by) || 'a member')
      + '  ·  ' + esc(ago(a.decided_at)) + '</span></span></div>').join('');
  }
}

function amendmentCard(a) {
  const pct = Math.round(Number(a.score) * 100);
  return '<article class="card pad stack" style="gap:16px">'
    + '<div class="row wrap" style="gap:10px">'
    + '<span class="glyph amber" aria-hidden="true">⇄</span>'
    + '<span class="grow"><span style="font-size:15px;font-weight:600">This looks like a revision</span>'
    + '<span class="sub">matched on ' + esc(a.matched_on) + '</span></span>'
    + '<span class="tag ' + (pct >= 70 ? 'green' : 'amber') + '">' + pct + '% match</span>'
    + '</div>'
    + '<div class="revision">'
    + '<div class="side"><div class="label">The newer notice says</div>'
    + '<a class="ref" href="#/notices/' + esc(a.artifact_id) + '">' + esc(a.claim_title) + '</a>'
    + (a.change ? '<p class="change">“' + esc(a.change) + '”</p>' : '')
    + '</div>'
    + '<div class="arrow" aria-hidden="true">replaces</div>'
    + '<div class="side old"><div class="label">The earlier notice</div>'
    + '<a class="ref" href="#/notices/' + esc(a.amends_artifact_id) + '">' + esc(a.amends_title) + '</a>'
    + (a.amends_date ? '<p class="change">was ' + esc(date(a.amends_date)) + '</p>' : '')
    + '</div></div>'
    + '<div class="row wrap" style="gap:10px">'
    + '<span class="muted" style="max-width:520px;line-height:1.5">Confirming supersedes the earlier notice’s '
    + 'tasks and stops their reminders. Rejecting leaves both standing.</span>'
    + '<span class="grow"></span>'
    + '<button class="btn ghost" data-decide="rejected" data-id="' + esc(a.id) + '">Not the same thing</button>'
    + '<button class="btn" data-decide="confirmed" data-id="' + esc(a.id) + '">Confirm the revision</button>'
    + '</div></article>';
}


/* ── household ────────────────────────────────────────────────── */

/* ── connected mailboxes, export, and what did not arrive ─────── */

function mailboxesCard(mailboxes) {
  const rows = mailboxes.map((m) => '<div class="row" style="gap:10px;padding:9px 11px;background:var(--sunk);'
    + 'border:1px solid var(--line-soft);border-radius:7px">'
    + '<span class="grow"><span style="font-size:12.5px;font-weight:600">' + esc(m.email) + '</span>'
    + '<span class="sub">' + (m.needs_reconnect
      ? 'Not being read — Google will not refresh this any more.'
      : 'connected ' + esc(ago(m.connected_at))
        + (m.last_polled_at ? '  ·  last read ' + esc(ago(m.last_polled_at)) : '  ·  not read yet')
        + (m.backfill_done ? '' : '  ·  still catching up')
        // The brief says a mailbox has gone quiet; this is where it says why.
        + (m.last_error ? '  ·  ' + esc(m.last_error) : '')) + '</span></span>'
    + (m.needs_reconnect ? '<span class="tag red">reconnect</span>'
      : m.last_error ? '<span class="tag amber">stalled</span>'
      : '<span class="tag green">reading</span>')
    + '<button class="btn ghost sm" data-disconnect="' + esc(m.id) + '">Disconnect</button>'
    + '</div>').join('');

  return '<div class="card pad stack" style="gap:10px">'
    + '<div class="row wrap" style="gap:8px"><h2>Connected mailboxes</h2>'
    + '<span class="muted">read-only</span></div>'
    + '<p class="muted" style="line-height:1.55">FamilyOS asks for one permission, <b>gmail.readonly</b>, and reads '
    + 'only mail that looks like a school notice. It cannot send, change or label anything. The token is sealed with '
    + 'your household key, so erasing the household also ends this.</p>'
    + (rows || '<p class="muted">Nothing connected. Forwarding keeps working either way.</p>')
    + '<button class="btn sm" data-connectmail style="align-self:flex-start">Connect a Gmail account</button>'
    + '<p class="muted" style="line-height:1.45">While FamilyOS is in testing with Google, a connection lasts seven '
    + 'days and then asks to be reconnected. Your brief says so when it happens, rather than quietly reading nothing.</p>'
    + '</div>';
}

function exportCard() {
  return '<div class="card pad stack" style="gap:9px">'
    + '<h3>Take everything with you</h3>'
    + '<p class="muted" style="line-height:1.55">Every notice you can see, exactly as it arrived, with a page for each '
    + 'one saying what was read out of it and the words it came from — plus the same contents as JSON. Built for '
    + 'the download and never stored.</p>'
    + '<button class="btn ghost" data-export>Download everything</button>'
    + '</div>';
}

const WHERE_LABEL = {
  email: 'Email we did not match',
  whatsapp_group: 'A WhatsApp group',
  whatsapp_direct: 'A WhatsApp message',
  school_portal: 'The school portal',
  school_app: 'The school app',
  other_app: 'Another app',
  sms: 'A text message',
  paper: 'On paper',
  word_of_mouth: 'Somebody told us',
  unknown: 'We are not sure',
};

function gapsCard(summary, gaps) {
  const rate = summary.capture_rate === null || summary.capture_rate === undefined
    ? '—' : Math.round(summary.capture_rate * 100) + '%';
  const bars = summary.missed_by_where.map((w) =>
    '<div class="row" style="gap:10px;align-items:flex-start">'
    + '<span class="lead" aria-hidden="true">' + w.count + '</span>'
    + '<span class="grow"><span style="font-size:12.5px;font-weight:600">'
    + esc(WHERE_LABEL[w.lived_where] || w.lived_where) + '</span>'
    + '<span class="sub">' + esc(w.means)
    + (w.with_a_date ? '  ·  ' + w.with_a_date + ' asked for something by a date' : '')
    + (w.arrived_later ? '  ·  ' + w.arrived_later + ' turned up later' : '') + '</span></span></div>').join('');

  return '<div class="card pad stack" style="gap:11px">'
    + '<div class="row wrap" style="gap:8px"><h2>What did not arrive</h2>'
    + '<span class="muted">' + summary.captured + ' in, ' + summary.missed_reported + ' missed</span></div>'
    + '<p class="muted" style="line-height:1.55">Capture <b style="color:var(--ink)">' + rate + '</b>. '
    + esc(summary.caveat) + '</p>'
    + (bars ? '<div class="stack" style="gap:9px">' + bars + '</div>'
      : '<p class="muted">Nothing reported missing. If something never reaches here, say so — where it '
        + 'actually lived is what decides what gets built next.</p>')
    + '<button class="btn ghost sm" data-reportgap style="align-self:flex-start">Report a missed notice</button>'
    + (gaps.length
      ? '<details><summary class="muted" style="cursor:pointer;font-size:12px">' + gaps.length
        + ' report' + (gaps.length === 1 ? '' : 's') + '</summary><div class="stack" style="gap:6px;padding-top:9px">'
        + gaps.map((g) => '<div class="row" style="gap:8px">'
          + '<span class="grow"><span style="font-size:12.5px">' + esc(g.title) + '</span>'
          + '<span class="sub">' + esc(WHERE_LABEL[g.lived_where] || g.lived_where)
          + '  ·  ' + esc(memberName(g.reported_by) || 'a member')
          + '  ·  ' + esc(ago(g.created_at)) + '</span></span>'
          + (g.arrived_as ? '<span class="tag green">turned up</span>' : '') + '</div>').join('')
        + '</div></details>'
      : '')
    + '</div>';
}

function gapDialog() {
  const options = Object.keys(WHERE_LABEL)
    .map((k) => '<option value="' + k + '">' + esc(WHERE_LABEL[k]) + '</option>').join('');
  return '<dialog class="card" data-gap style="max-width:460px;padding:20px;border:1px solid var(--line)">'
    + '<form class="stack" data-form="gap"><h2>Something never reached here</h2>'
    + '<p class="muted" style="line-height:1.55">This is counted, not read: it never becomes a task or a date, '
    + 'because what you type here is from memory and FamilyOS only records what it can point at in a document. '
    + 'Where it lived is the useful part.</p>'
    + '<label>What was it?<input type="text" name="title" required maxlength="300" '
    + 'placeholder="The swimming letter"></label>'
    + '<label>Where did it actually live?<select name="lived_where">' + options + '</select></label>'
    + '<label>Was it emailed as well?<select name="also_emailed">'
    + '<option value="">Not sure</option><option value="no">No</option><option value="yes">Yes</option>'
    + '</select></label>'
    + '<label class="row" style="gap:8px;align-items:center"><input type="checkbox" name="had_date" '
    + 'style="width:auto">It asked for something by a date</label>'
    + '<label>When did you find out? (optional)<input type="date" name="noticed_on"></label>'
    + '<div class="row" style="justify-content:flex-end"><button class="btn ghost" type="button" data-close-gap>Cancel</button>'
    + '<button class="btn" type="submit">Report it</button></div></form></dialog>';
}

async function download(path, fallbackName) {
  flash('Building it…');
  const blob = await api(path, { blob: true });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = fallbackName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoked straight away: the whole household is in that blob.
  setTimeout(() => URL.revokeObjectURL(url), 4000);
  flash('Downloaded.');
}

function wireConnections(main) {
  const connect = main.querySelector('[data-connectmail]');
  if (connect) connect.addEventListener('click', async () => {
    try {
      const begun = await api('/google/mailboxes/authorize', { method: 'POST' });
      // Same tab: Google sends them back to the callback, which returns here.
      location.href = begun.url;
    } catch (err) { fail(err); }
  });

  main.querySelectorAll('[data-disconnect]').forEach((b) => b.addEventListener('click', async () => {
    if (!confirm('Stop reading this mailbox? The stored token is overwritten and Google is told.')) return;
    try {
      await api('/google/mailboxes/' + b.dataset.disconnect + '/disconnect', { method: 'POST' });
      flash('Disconnected.');
      await render();
    } catch (err) { fail(err); }
  }));

  const exp = main.querySelector('[data-export]');
  if (exp) exp.addEventListener('click', async () => {
    try {
      await download('/export', 'familyos-export.zip');
    } catch (err) { fail(err); }
  });
}

function wireGaps(main) {
  const dlg = main.querySelector('dialog[data-gap]');
  if (!dlg) return;
  main.querySelectorAll('[data-reportgap]').forEach((b) =>
    b.addEventListener('click', () => dlg.showModal()));
  dlg.querySelector('[data-close-gap]').addEventListener('click', () => dlg.close());
  dlg.querySelector('form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const also = f.get('also_emailed');
    const body = {
      title: f.get('title'),
      lived_where: f.get('lived_where'),
      had_date: f.get('had_date') === 'on',
    };
    if (also) body.also_emailed = also === 'yes';
    if (f.get('noticed_on')) body.noticed_on = f.get('noticed_on');
    try {
      await api('/intake-gaps', { method: 'POST', json: body });
      dlg.close();
      ev.target.reset();
      flash('Logged. Where it lived is what decides what gets built next.');
      await render();
    } catch (err) { fail(err); }
  });
}

/* ── household ────────────────────────────────────────────────── */

async function householdView(main) {
  const [household, consents, mySessions, mailboxes, summary, gaps] = await Promise.all([
    api('/household'), api('/consents'), api('/sessions'),
    api('/google/mailboxes'), api('/intake-gaps/summary'), api('/intake-gaps'),
  ]);
  store.household = household;
  store.mailboxes = mailboxes;
  const isGuardian = store.me.role === 'guardian';
  const children = (household.members || []).filter((m) => m.role === 'child');
  const active = (id) => consents.find((c) => c.subject_member_id === id && !c.withdrawn_at);

  main.innerHTML = '<div class="head"><div><h1>' + esc(household.name) + '</h1>'
    + '<p>Guardians manage the household and consent for the children. Children are not accounts and do not sign in.</p></div>'
    + (isGuardian ? '<button class="btn" data-addmember>Add a member</button>' : '') + '</div>'
    + '<div class="split"><div class="stack">'
    + '<div class="card list" data-members></div>'
    + '<div class="card pad stack" data-consent></div>'
    + '</div><div class="stack">'
    + mailboxesCard(mailboxes)
    + gapsCard(summary, gaps)
    + sessionsCard(mySessions)
    + exportCard()
    + (isGuardian ? dangerCard(household) : '')
    + '</div></div>'
    + memberDialog()
    + gapDialog();

  main.querySelector('[data-members]').innerHTML = (household.members || []).map((m) => {
    const tone = m.role === 'guardian' ? 'blue' : m.role === 'child' ? 'green' : '';
    const c = active(m.id);
    return '<div class="item" style="cursor:default">'
      + '<span class="glyph ' + tone + '">' + esc(initial(m.display_name)) + '</span>'
      + '<span class="grow"><span class="row wrap" style="gap:8px"><span class="title">' + esc(m.display_name) + '</span>'
      + '<span class="tag ' + tone + '">' + esc(m.role) + '</span>'
      + (m.id === store.me.id ? '<span class="tag line">you</span>' : '') + '</span>'
      + '<span class="sub">' + esc(m.email || 'no account — a data subject only')
      + (m.date_of_birth ? '  ·  born ' + esc(date(m.date_of_birth)) : '') + '</span></span>'
      + (m.role === 'child'
        ? (c ? '<span class="tag kid">consented</span>' : '<span class="tag red">no consent</span>') : '')
      + (m.role === 'child' && isGuardian
        ? '<button class="btn danger sm" data-erase="' + esc(m.id) + '">Erase</button>' : '')
      + (m.role !== 'child' && (isGuardian || m.id === store.me.id)
        ? '<button class="btn ghost sm" data-signoutall="' + esc(m.id) + '">Sign out everywhere</button>' : '')
      + '</div>';
  }).join('');

  const cbox = main.querySelector('[data-consent]');
  cbox.innerHTML = '<div class="row wrap" style="gap:9px"><h2>Consent for the children</h2>'
    + '<span class="muted">DPDP Act 2023, s.9 · kept as evidence even after it ends</span></div>'
    + (children.length ? '<div class="stack" style="gap:8px" data-crows></div>'
      : '<p class="muted">No children in this household yet.</p>')
    + '<p class="muted" style="padding:11px 13px;background:var(--red-tint);border-radius:7px;color:#6e2222;line-height:1.55">'
    + 'Withdrawing consent starts an erasure in the same transaction. Every notice naming that child goes, along with '
    + 'anything read from it. The consent record itself stays, as proof it existed and ended.</p>';

  if (children.length) {
    cbox.querySelector('[data-crows]').innerHTML = children.map((m) => {
      const c = active(m.id);
      return '<div class="row card" style="gap:12px;padding:11px 13px;background:var(--sunk)">'
        + '<span class="glyph green" style="width:30px;height:30px;border-radius:15px">' + esc(initial(m.display_name)) + '</span>'
        + '<span class="grow"><span style="font-size:13.5px;font-weight:600">' + esc(m.display_name) + '</span>'
        + '<span class="sub">' + (c
          ? 'by ' + esc(memberName(c.guardian_member_id) || 'a guardian') + '  ·  verified as '
            + esc(c.verification_method) + '  ·  notice ' + esc(c.notice_version) + '  ·  ' + esc(date(c.granted_at))
          : 'Nothing may name this child until a guardian consents.') + '</span></span>'
        + (c ? '<span class="tag green">active</span>' : '')
        + (isGuardian
          ? (c ? '<button class="btn danger sm" data-withdraw="' + esc(c.id) + '">Withdraw</button>'
            : '<button class="btn sm" data-grant="' + esc(m.id) + '">Record consent</button>') : '')
        + '</div>';
    }).join('');
  }

  wireHousehold(main, household);
  wireConnections(main);
  wireGaps(main);

  // The Google callback returns to #/household?mailbox=..., which is the only
  // place the result of leaving the app can be reported.
  const said = hashQuery().get('mailbox');
  if (said) {
    history.replaceState(null, '', '#/household');
    if (said === 'connected') flash('Mailbox connected. School mail in it will start arriving.');
    else if (said === 'cancelled') flash('Not connected \u2014 nothing was shared.');
    else flash('That did not connect. Nothing was stored; try again.', true);
  }
}

function sessionsCard(sessions) {
  return '<div class="card pad stack" style="gap:10px">'
    + '<div class="row wrap" style="gap:8px"><h2>Your sign-ins</h2>'
    + '<span class="muted">' + sessions.length + ' live</span></div>'
    + '<p class="muted" style="line-height:1.55">A token lasts 30 days and slides forward while you use it. '
    + 'Signing out ends the one you are on; other devices stay signed in.</p>'
    + sessions.map((s) =>
      '<div class="row" style="gap:10px;padding:9px 11px;background:var(--sunk);border:1px solid var(--line-soft);border-radius:7px">'
      + '<span class="grow"><span style="font-size:12.5px;font-weight:600">'
      + (s.current ? 'This device' : 'Another device') + '</span>'
      + '<span class="sub">started ' + esc(ago(s.created_at))
      + (s.last_used_at ? '  \u00B7  last used ' + esc(ago(s.last_used_at)) : '')
      + (s.expires_at ? '  \u00B7  ends ' + esc(date(s.expires_at)) : '') + '</span></span>'
      + (s.current ? '<span class="tag green">current</span>' : '') + '</div>').join('')
    + '</div>';
}

function dangerCard(household) {
  return '<div class="card pad stack" style="gap:9px;border-color:#c99494">'
    + '<h3 style="color:var(--red)">Erase everything</h3>'
    + '<p class="muted" style="line-height:1.55">Sign-in and intake stop at once. The household key is destroyed first, so '
    + 'every copy of your originals — backups included — is unreadable before a single file is deleted.</p>'
    + '<button class="btn danger" data-erasehousehold data-name="' + esc(household.name) + '">Erase the household…</button>'
    + '</div>';
}

function memberDialog() {
  return '<dialog class="card" style="max-width:420px;padding:20px;border:1px solid var(--line)">'
    + '<form class="stack" data-form="member"><h2>Add a member</h2>'
    + '<label>Name<input type="text" name="display_name" required maxlength="100"></label>'
    + '<label>Role<select name="role"><option value="adult">Adult — signs in, sees what is shared</option>'
    + '<option value="guardian">Guardian — can consent and manage</option>'
    + '<option value="child">Child — a data subject, no account</option></select></label>'
    + '<label data-emailwrap>Email<input type="email" name="email"></label>'
    + '<label data-dobwrap hidden>Date of birth (optional)<input type="date" name="date_of_birth"></label>'
    + '<div class="row" style="justify-content:flex-end"><button class="btn ghost" type="button" data-close>Cancel</button>'
    + '<button class="btn" type="submit">Add</button></div></form></dialog>';
}

function wireHousehold(main, household) {
  const dlg = main.querySelector('dialog');
  const add = main.querySelector('[data-addmember]');
  if (add) add.addEventListener('click', () => dlg.showModal());
  dlg.querySelector('[data-close]').addEventListener('click', () => dlg.close());
  const roleSel = dlg.querySelector('[name="role"]');
  roleSel.addEventListener('change', () => {
    const child = roleSel.value === 'child';
    dlg.querySelector('[data-emailwrap]').hidden = child;
    dlg.querySelector('[name="email"]').required = !child;
    dlg.querySelector('[data-dobwrap]').hidden = !child;
  });
  dlg.querySelector('[name="email"]').required = true;

  dlg.querySelector('form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const body = { display_name: f.get('display_name'), role: f.get('role') };
    if (body.role !== 'child') body.email = f.get('email');
    if (body.role === 'child' && f.get('date_of_birth')) body.date_of_birth = f.get('date_of_birth');
    try {
      const made = await api('/household/members', { method: 'POST', json: body });
      dlg.close();
      if (made.token) {
        flash('Added. Their token: ' + made.token);
        window.prompt('This token is shown once. Copy it and give it to them:', made.token);
      } else {
        flash('Added. Children do not sign in.');
      }
      await render();
    } catch (err) { fail(err); }
  });

  main.querySelectorAll('[data-grant]').forEach((b) => b.addEventListener('click', async () => {
    const notice = window.prompt('Which consent notice version are you recording against?', new Date().toISOString().slice(0, 7));
    if (!notice) return;
    try {
      await api('/consents', {
        method: 'POST',
        json: {
          subject_member_id: b.dataset.grant,
          purpose: 'household_records',
          notice_version: notice,
          verification_method: 'account_holder',
        },
      });
      flash('Consent recorded.');
      await render();
    } catch (err) { fail(err); }
  }));

  main.querySelectorAll('[data-withdraw]').forEach((b) => b.addEventListener('click', async () => {
    if (!confirm('Withdraw consent? Every notice naming this child, and everything read from it, is erased. This cannot be undone.')) return;
    try {
      const e = await api('/consents/' + b.dataset.withdraw + '/withdraw', { method: 'POST' });
      flash('Withdrawn. Erasure ' + e.id.slice(0, 8) + ' is ' + e.status + '.');
      await render();
    } catch (err) { fail(err); }
  }));

  main.querySelectorAll('[data-signoutall]').forEach((b) => b.addEventListener('click', async () => {
    const mine = b.dataset.signoutall === store.me.id;
    if (!confirm(mine
      ? 'Sign out on every device, including this one?'
      : 'End every sign-in this member has? They will need a new token to get back in.')) return;
    try {
      const out = await api('/members/' + b.dataset.signoutall + '/signout', { method: 'POST' });
      flash(out.ended + ' sign-in' + (out.ended === 1 ? '' : 's') + ' ended.');
      if (mine) { forget(); return; }
      await render();
    } catch (err) { fail(err); }
  }));

  main.querySelectorAll('[data-erase]').forEach((b) => b.addEventListener('click', async () => {
    if (!confirm('Erase this child’s data and remove them from the household? This cannot be undone.')) return;
    try {
      const e = await api('/members/' + b.dataset.erase + '/erase', { method: 'POST' });
      flash('Erasure ' + e.id.slice(0, 8) + ' is ' + e.status + '.');
      await render();
    } catch (err) { fail(err); }
  }));

  const nuke = main.querySelector('[data-erasehousehold]');
  if (nuke) {
    nuke.addEventListener('click', async () => {
      const typed = window.prompt('This erases everything, for everyone, and cannot be undone.\n\nType the household name exactly to confirm:');
      if (typed === null) return;
      try {
        const e = await api('/household/erase', { method: 'POST', json: { confirm_name: typed } });
        flash('Erasure ' + e.id.slice(0, 8) + ' started. Every token is now invalid.');
        setTimeout(forget, 2500);
      } catch (err) { fail(err); }
    });
  }
}

/* ── audit ────────────────────────────────────────────────────── */

async function auditView(main) {
  const events = await api('/audit?limit=200');
  main.innerHTML = '<div class="head"><div><div class="row" style="gap:10px"><h1>Audit</h1>'
    + '<span class="tag">append-only</span></div>'
    + '<p>Who did what to which record. The table refuses UPDATE and DELETE; only a household erasure may purge it. '
    + 'Entries carry ids, counts, sizes and hashes — never filenames, subjects or text.</p></div></div>'
    + (events.length ? '<div class="card list" data-rows></div>'
      : '<div class="card"><div class="empty">No events yet.</div></div>');
  if (!events.length) return;

  main.querySelector('[data-rows]').innerHTML = events.map((e) => {
    const detail = Object.keys(e.detail || {})
      .map((k) => k + ' ' + JSON.stringify(e.detail[k]))
      .join('   ');
    return '<div class="item" style="cursor:default;align-items:flex-start">'
      + '<span class="muted" style="width:96px;flex-shrink:0">' + esc(ago(e.at)) + '</span>'
      + '<span class="grow"><span class="mono" style="font-size:12.5px;font-weight:500">' + esc(e.action) + '</span>'
      + '<span class="sub mono" style="font-size:11px">' + esc(detail || '—') + '</span></span>'
      + '<span class="tag line" style="flex-shrink:0">' + esc(e.actor_kind) + '</span>'
      + '<span class="muted" style="width:110px;text-align:right;flex-shrink:0">'
      + esc(e.actor_member_id ? memberName(e.actor_member_id) : '') + '</span>'
      + '</div>';
  }).join('');
}

/* ── start ────────────────────────────────────────────────────── */

window.addEventListener('hashchange', () => { render().catch(() => {}); });

store.token = readToken();
if (store.token) {
  boot().catch(() => { store.token = null; writeToken(null); gateView(); });
} else {
  gateView();
}
