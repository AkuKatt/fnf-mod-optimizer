/* FNF Mod Optimizer - web UI. Plain JavaScript, no libraries.
   Rules: every piece of untrusted text (file names, paths, tool output)
   goes in through textContent / createTextNode, never innerHTML. */
'use strict';
(() => {

// ======================================================================
// helpers
// ======================================================================
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const SVGNS = 'http://www.w3.org/2000/svg';

function add(parent, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    parent.append(k.nodeType ? k : document.createTextNode(String(k)));
  }
  return parent;
}
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else if (v === true) e.setAttribute(k, '');
      else e.setAttribute(k, v);
    }
  }
  return add(e, kids);
}
const LOGO_MARK = '<path class="mk-l" d="M3.5 12.2 7.2 16l-3.7 3.8"/><path class="mk-r" d="M28.5 12.2 24.8 16l3.7 3.8"/><path class="mk-a" d="M16 6.5 22.6 14.6h-4.2V24h-4.8v-9.4H9.4z"/>';
function logoEl() {
  const wrap = document.createElement('span');
  wrap.className = 'logo';
  wrap.setAttribute('aria-hidden', 'true');
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('viewBox', '0 0 32 32');
  svg.setAttribute('class', 'mark');
  svg.innerHTML = LOGO_MARK;
  wrap.append(svg);
  return wrap;
}
function icon(name, cls) {
  const s = document.createElementNS(SVGNS, 'svg');
  s.setAttribute('class', 'i' + (cls ? ' ' + cls : ''));
  s.setAttribute('aria-hidden', 'true');
  const u = document.createElementNS(SVGNS, 'use');
  u.setAttribute('href', '#i-' + name);
  s.append(u);
  return s;
}
const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };
const isObj = (v) => v && typeof v === 'object';
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ======================================================================
// language
// ======================================================================
let LANG = 'en';
const DICT = window.I18N;
function has(key) { return key in DICT[LANG] || key in DICT.en; }
function t(key, params) {
  let s = DICT[LANG][key];
  if (s === undefined) s = DICT.en[key];
  if (s === undefined) s = key;
  if (params) s = s.replace(/\{(\w+)\}/g, (m, k) => (params[k] != null ? params[k] : m));
  return s;
}
const plural = (base, n) => t(base + (n === 1 ? '.one' : '.other'), { n: fmtNum(n) });
function fmtNum(n, digits = 0) {
  return new Intl.NumberFormat(LANG === 'id' ? 'id-ID' : 'en-US', { maximumFractionDigits: digits }).format(n);
}
function fmtBytes(n) {
  n = Number(n) || 0;
  const neg = n < 0; n = Math.abs(n);
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (neg ? '-' : '') + fmtNum(n, i === 0 ? 0 : 1) + ' ' + units[i];
}
function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
  if (hh) return `${hh}h ${String(mm).padStart(2, '0')}m`;
  if (mm) return `${mm}m ${String(ss).padStart(2, '0')}s`;
  return `${ss}s`;
}
const fmtPct = (x) => fmtNum(x, x >= 10 ? 0 : 1) + '%';

// ======================================================================
// app state
// ======================================================================
const S = {
  app: null, deps: [], install: { running: false, state: 'idle' }, gpu: { available: false, encoders: [] },
  prefs: { language: 'en', theme: 'system', reduce_motion: false, backup: true, gpu: true, workers: 'auto', parallel_mode: 'process', welcomed: false, last_path: '' },
  settings: { schema: [], values: {}, groups: [], prefs_schema: [] },
  job: null, scan: null, results: null, resultDismissed: false,
  view: 'optimize', sub: '',
  conn: true, pathInput: '', scanError: null, scanning: false, starting: false,
  resultFilter: 'all', resultQuery: '',
  openSections: new Set(['app']), settingsQuery: '',
  setupQueue: null, setupOpen: false,
};
let TOKEN = '';

// ======================================================================
// network
// ======================================================================
class ApiError extends Error {
  constructor(status, data) { super((data && data.error) || 'HTTP ' + status); this.status = status; this.data = data || {}; this.code = this.data.code; }
}
class NetError extends Error { constructor() { super('network'); this.net = true; } }

async function api(path, body) {
  const init = { method: body !== undefined ? 'POST' : 'GET', headers: { 'X-Token': TOKEN } };
  if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, init); } catch (e) { setConn(false); throw new NetError(); }
  setConn(true);
  let data = null;
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (res.status === 401) showTokenProblem();
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}
function errText(e) {
  if (e instanceof NetError) return t('conn.lost');
  if (e instanceof ApiError) {
    const code = e.code;
    const base = code && has('err.' + code) ? t('err.' + code) : (e.data && e.data.error) || t('err.unknown');
    return code === 'invalid' && e.data.error ? `${base} (${e.data.error})` : base;
  }
  return t('err.unknown');
}

// ======================================================================
// toasts
// ======================================================================
function toast(message, kind = 'info', ms) {
  const box = $('#toasts');
  while (box.children.length >= 3) box.firstChild.remove();
  const el = h('div', { class: 'toast ' + kind, role: kind === 'err' ? 'alert' : null },
    icon({ ok: 'check', err: 'x', warn: 'warn', info: 'info' }[kind] || 'info'), h('span', { text: message }));
  const kill = () => { if (!el.isConnected || el.classList.contains('out')) return; el.classList.add('out'); setTimeout(() => el.remove(), 260); };
  el.addEventListener('click', kill);
  box.append(el);
  setTimeout(kill, ms || (kind === 'err' ? 7000 : 4000));
}

// ======================================================================
// connection banner
// ======================================================================
function setConn(ok) {
  if (ok === S.conn) return;
  S.conn = ok;
  const b = $('#conn-banner');
  if (!ok) {
    clear(b);
    add(b, [icon('warn'), h('div', { class: 'grow' }, h('div', { text: t('conn.lost') }),
      h('div', { class: 'actions' }, h('button', { class: 'btn sm', onclick: () => pollNow(), text: t('conn.retry') })))]);
    b.hidden = false;
  } else {
    b.hidden = true;
    toast(t('conn.back'), 'ok', 2200);
  }
}
function showTokenProblem() {
  const b = $('#conn-banner');
  clear(b);
  add(b, [icon('warn'), h('div', { class: 'grow', text: t('conn.token') })]);
  b.hidden = false;
}

// ======================================================================
// preferences: language / theme / motion
// ======================================================================
const mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
function applyAppearance() {
  const root = document.documentElement;
  cachePrefs();
  const theme = S.prefs.theme;
  root.dataset.theme = theme;
  root.dataset.resolved = theme === 'system' ? (mq && mq.matches ? 'dark' : 'light') : theme;
  root.dataset.motion = S.prefs.reduce_motion ? 'reduced' : 'full';
  root.lang = LANG;
}
if (mq && mq.addEventListener) mq.addEventListener('change', applyAppearance);

function translateStatic() {
  $$('[data-i18n]').forEach((n) => { n.textContent = t(n.dataset.i18n); });
  $('#skip-link').textContent = t('skip');
  $('#nav').setAttribute('aria-label', t('nav.main'));
  $('#lang-switch').setAttribute('aria-label', t('nav.lang'));
  $('#log-title').textContent = t('log.title');
  $('#log-filter button[data-f="all"]').textContent = t('log.all');
  $('#log-filter button[data-f="problems"]').textContent = t('log.problems');
  $('#log-copy .lbl').textContent = t('common.copy');
  $('#log-clear .lbl').textContent = t('common.clear');
  $('#log-copy').setAttribute('aria-label', t('log.copy'));
  $('#log-clear').setAttribute('aria-label', t('log.clear'));
  const sw = $('#lang-switch');
  clear(sw);
  for (const [code, info] of Object.entries(window.LANGS)) {
    sw.append(h('button', {
      type: 'button', 'aria-pressed': String(code === LANG), lang: code, title: info.name,
      onclick: () => setLanguage(code),
    }, info.flag, h('span', { class: 'code', text: ' ' + info.short })));
  }
  const v = S.app ? 'v' + S.app.version : '';
  $('#ver').textContent = v;
  updateLogChrome();
}

function setLanguage(code, persist = true) {
  if (!DICT[code]) code = 'en';
  const changed = code !== LANG;
  const apply = () => {
    LANG = code; S.prefs.language = code;
    applyAppearance(); translateStatic(); renderAll(); rerenderLog();
    if (persist && changed) toast(t('toast.lang'), 'info', 1800);     // after the switch, so it speaks the new language
  };
  if (changed && S.app) morph(apply, 'lang'); else apply();
  if (persist && changed) api('/api/prefs', { changes: { language: code } }).catch(() => {});
}

// ======================================================================
// router
// ======================================================================
const VIEWS = ['optimize', 'settings', 'about'];
function parseHash() {
  const parts = location.hash.replace(/^#\/?/, '').split('/');
  const view = VIEWS.includes(parts[0]) ? parts[0] : 'optimize';
  return { view, sub: parts[1] || '' };
}
function go(view, sub) { location.hash = '#/' + view + (sub ? '/' + sub : ''); }

// ---------- motion: View Transitions (composited on the GPU) ----------
const idle = (fn) => (window.requestIdleCallback ? window.requestIdleCallback(fn, { timeout: 1500 }) : setTimeout(fn, 120));
const reducedMotion = () => S.prefs.reduce_motion === true || !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
let lastPtr = { x: window.innerWidth / 2, y: window.innerHeight / 2 };
function morph(update, kind, dir) {
  const root = document.documentElement;
  if (!document.startViewTransition || reducedMotion()) { update(); return null; }
  kind = kind || 'page';
  root.dataset.vt = kind;
  if (dir) root.dataset.dir = dir;
  if (kind === 'theme') {
    const r = Math.hypot(Math.max(lastPtr.x, window.innerWidth - lastPtr.x), Math.max(lastPtr.y, window.innerHeight - lastPtr.y));
    root.style.setProperty('--vt-x', lastPtr.x + 'px'); root.style.setProperty('--vt-y', lastPtr.y + 'px'); root.style.setProperty('--vt-r', r + 'px');
  }
  let vt;
  try { vt = document.startViewTransition(() => { update(); }); } catch (e) { update(); return null; }
  const done = () => { if (root.dataset.vt === kind) delete root.dataset.vt; };
  vt.ready.catch(() => {}); vt.finished.then(done, done);
  return vt;
}

// the sliding pill behind the active navigation item (it morphs between items)
function movePill(animate) {
  const nav = $('#nav'), pill = $('#nav-pill'), cur = $('#nav a[aria-current="page"] .nav-ind');
  if (!nav || !pill || !cur) return;
  const n = nav.getBoundingClientRect(), r = cur.getBoundingClientRect();
  const x = Math.round(r.left - n.left - nav.clientLeft), y = Math.round(r.top - n.top - nav.clientTop);
  const from = pill._pos; pill._pos = { x, y };
  const to = `translate(${x}px, ${y}px)`;
  pill.style.transform = to; pill.hidden = false;
  if (animate && from && !reducedMotion() && pill.animate && (from.x !== x || from.y !== y)) {
    const horizontal = getComputedStyle(nav).flexDirection.startsWith('row');
    const mid = `translate(${(from.x + x) / 2}px, ${(from.y + y) / 2}px) scale(${horizontal ? 1.9 : 0.85}, ${horizontal ? 0.8 : 1.9})`;
    pill.animate([{ transform: `translate(${from.x}px, ${from.y}px)` }, { transform: mid, offset: 0.45 }, { transform: to }],
      { duration: 480, easing: 'cubic-bezier(.2,0,0,1)' });
  }
}
window.addEventListener('resize', () => movePill(false));
const VIEW_ORDER = ['optimize', 'settings', 'about'];
let prevView = null;
const stale = { settings: true, about: true };       // Settings/About are built once and reused
function route(focus) {
  const { view, sub } = parseHash();
  const first = prevView === null, changed = view !== prevView;
  const dir = !first && VIEW_ORDER.indexOf(view) < VIEW_ORDER.indexOf(prevView) ? 'back' : 'fwd';
  S.view = view; S.sub = sub;
  document.title = `${t('nav.' + view)} · FNF Mod Optimizer`;
  if (view === 'settings' && sub === 'deps') S.openSections.add('deps');
  const apply = () => {
    for (const v of VIEWS) $('#view-' + v).hidden = v !== view;
    $$('#nav a').forEach((a) => {
      if (a.dataset.view === view) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    renderView(view);
    movePill(!first && changed);
    if (view === 'settings' && sub) {
      requestAnimationFrame(() => { const el = $('#sec-' + sub); if (el) el.scrollIntoView({ block: 'start', behavior: 'smooth' }); });
    }
    if (focus) { const m = $('#main'); m.focus({ preventScroll: true }); window.scrollTo({ top: 0 }); }
  };
  prevView = view;
  if (first || !changed) apply(); else morph(apply, 'page', dir);
}
function renderView(view) {
  if (view === 'optimize') { renderOptimize(); return; }
  if (!stale[view]) return;                          // already built: switching is instant
  if (view === 'settings') renderSettings(); else renderAbout();
}
function prewarm() {                                 // build the other pages while the browser is idle
  idle(() => {
    if (!S.app) return;
    if (stale.settings) renderView('settings');
    idle(() => { if (S.app && stale.about) renderView('about'); });
  });
}
function renderAll() {
  stale.settings = stale.about = true;
  renderView(S.view);
  updateNavDot();
  updateSetupOverlay();
  prewarm();
}
function updateNavDot() {
  const need = S.deps.some((d) => d.role === 'required' && !d.installed);
  $('#nav-dot').hidden = !need;
}

// ======================================================================
// process log panel
// ======================================================================
const LOG = { lines: [], last: 0, filter: 'all', open: false, counts: { warn: 0, error: 0 }, userCollapsed: false };
const LOG_KEEP = 1500, LOG_DOM = 600;

function fmtLogArgs(a) {
  const o = Object.assign({}, a || {});
  for (const k of ['before', 'after', 'saved', 'size']) if (typeof o[k] === 'number') o[k] = fmtBytes(o[k]);
  if (o.role && has('dep.role.' + o.role)) o.role = t('dep.role.' + o.role);
  if (o.what && has('dep.what.' + o.what)) o.what = t('dep.what.' + o.what);
  if (o.version == null) o.version = '';
  return o;
}
function logText(e) {
  if (e.key && has(e.key)) return t(e.key, fmtLogArgs(e.args)).replace(/\s+$/, '');
  return e.text;
}
const logCls = (lvl) => (lvl === 'error' ? 'err' : lvl);
const logPasses = (e) => LOG.filter === 'all' || e.level === 'warn' || e.level === 'error';
function logTime(ts) {
  const d = new Date(ts * 1000);
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
}
function lineNode(e) {
  return h('div', { class: 'ln ' + logCls(e.level) },
    h('span', { class: 'tm', text: logTime(e.t) }), h('span', { class: 'tx', text: logText(e) }));
}
function logEmpty() { return h('div', { class: 'log-empty', 'data-empty': '1', text: t('log.empty') }); }

function rerenderLog() {
  const body = $('#log-body');
  clear(body);
  const shown = LOG.lines.filter(logPasses).slice(-LOG_DOM);
  if (!shown.length) body.append(logEmpty());
  else { const frag = document.createDocumentFragment(); shown.forEach((e) => frag.append(lineNode(e))); body.append(frag); }
  body.scrollTop = body.scrollHeight;
}
function ingestLog(data) {
  if (!data) return;
  LOG.counts = data.counts || LOG.counts;
  const fresh = (data.lines || []).filter((l) => l.id > LOG.last);
  if (fresh.length) {
    LOG.last = fresh[fresh.length - 1].id;
    LOG.lines.push(...fresh);
    if (LOG.lines.length > LOG_KEEP) LOG.lines.splice(0, LOG.lines.length - LOG_KEEP);
    const body = $('#log-body');
    const show = fresh.filter(logPasses);
    if (show.length) {
      const stick = body.scrollTop + body.clientHeight >= body.scrollHeight - 40;
      const empty = $('[data-empty]', body); if (empty) empty.remove();
      const frag = document.createDocumentFragment();
      show.forEach((e) => frag.append(lineNode(e)));
      body.append(frag);
      while (body.childElementCount > LOG_DOM) body.firstElementChild.remove();
      if (stick) body.scrollTop = body.scrollHeight;
    }
  }
  updateLogChrome();
}
function depName(id) { const d = S.deps.find((x) => x.id === id); return d ? d.name : id || ''; }

function logStatus() {
  const inst = S.install, job = S.job;
  if (inst && inst.running) {
    const pct = inst.progress == null ? null : inst.progress;
    return { text: t('log.busy.install', { name: depName(inst.dep), pct: pct == null ? '…' : fmtPct(pct * 100) }), busy: true, pct };
  }
  if (job && job.running) {
    if (job.phase === 'backup') return { text: t('log.busy.backup'), busy: true, pct: null };
    const pct = job.total ? job.processed / job.total : 0;
    return { text: t('log.busy.opt', { pct: fmtPct(pct * 100) }), busy: true, pct };
  }
  return { text: t('log.idle'), busy: false, pct: 0 };
}
function updateLogChrome() {
  const st = logStatus();
  $('#log-status').textContent = st.text;
  document.body.classList.toggle('is-busy', !!st.busy);
  const dot = $('#log-dot');
  dot.className = 'dotl' + (st.busy ? ' busy' : LOG.counts.error ? ' err' : LOG.counts.warn ? ' warn' : '');
  const prog = $('#log-progress');
  prog.classList.toggle('indeterminate', st.busy && st.pct == null);
  prog.firstElementChild.style.width = st.busy && st.pct != null ? Math.round(st.pct * 100) + '%' : (st.busy ? '' : '0%');
  const w = $('#log-warn'), e = $('#log-err');
  w.hidden = !LOG.counts.warn; e.hidden = !LOG.counts.error;
  w.textContent = plural('log.warnings', LOG.counts.warn);
  e.textContent = plural('log.errors', LOG.counts.error);
  $('#log-toggle').setAttribute('aria-label', (LOG.open ? t('log.collapse') : t('log.expand')) + ' — ' + st.text);
}
function setLogOpen(open, byUser) {
  LOG.open = open;
  if (byUser) { LOG.userCollapsed = !open; try { localStorage.setItem('fnf.logOpen', open ? '1' : '0'); } catch (e) { /* private mode */ } }
  const panel = $('#logpanel');
  panel.classList.toggle('open', open);
  $('#log-toggle').setAttribute('aria-expanded', String(open));
  const px = open ? Math.round(clamp(window.innerHeight * 0.36, 210, 340)) : 46;
  document.documentElement.style.setProperty('--log-h', px + 'px');
  updateLogChrome();
  if (open) { const b = $('#log-body'); b.scrollTop = b.scrollHeight; }
}
function ensureLogOpen() { if (!LOG.open && !LOG.userCollapsed) setLogOpen(true, false); }
function setLogFilter(f) {
  LOG.filter = f;
  $$('#log-filter button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.f === f)));
  rerenderLog();
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fall through */ }
  try {
    const ta = h('textarea', { 'aria-hidden': 'true', readonly: true }); ta.value = text;
    ta.className = 'sr-only'; document.body.append(ta); ta.select();
    const ok = document.execCommand('copy'); ta.remove(); return ok;
  } catch (e) { return false; }
}
async function copyWithToast(text, msgKey) {
  const ok = await copyText(text);
  toast(ok ? t(msgKey || 'toast.copied') : t('toast.copy_fail'), ok ? 'ok' : 'warn', 2200);
}
function initLogPanel() {
  $('#log-toggle').addEventListener('click', () => setLogOpen(!LOG.open, true));
  $('#log-filter').addEventListener('click', (ev) => { const b = ev.target.closest('button[data-f]'); if (b) setLogFilter(b.dataset.f); });
  $('#log-warn').addEventListener('click', () => { setLogFilter('problems'); setLogOpen(true, true); });
  $('#log-err').addEventListener('click', () => { setLogFilter('problems'); setLogOpen(true, true); });
  $('#log-copy').addEventListener('click', () => {
    const text = LOG.lines.filter(logPasses).map((e) => `[${logTime(e.t)}] ${e.level.toUpperCase().padEnd(5)} ${logText(e)}`).join('\n');
    copyWithToast(text, 'log.copied');
  });
  $('#log-clear').addEventListener('click', async () => {
    try { await api('/api/log/clear', {}); } catch (e) { /* ignore */ }
    LOG.lines = []; LOG.counts = { warn: 0, error: 0 }; rerenderLog(); updateLogChrome();
  });
  window.addEventListener('resize', () => { if (LOG.open) setLogOpen(true, false); });
  let stored = null; try { stored = localStorage.getItem('fnf.logOpen'); } catch (e) { /* ignore */ }
  setLogOpen(stored === '1', false);
  if (stored === '0') LOG.userCollapsed = true;
  rerenderLog();
}

// ======================================================================
// polling: one request carries log lines + job + install state
// ======================================================================
let pollTimer = 0, polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const ev = await api('/api/events?since=' + LOG.last);
    ingestLog(ev.log);
    applyInstall(ev.install);
    applyJob(ev.job);
  } catch (e) { /* the banner already explains a lost connection */ }
  finally { polling = false; schedule(); }
}
function schedule() {
  clearTimeout(pollTimer);
  const active = (S.job && S.job.running) || S.install.running;
  let ms = active ? 600 : 3000;
  if (document.hidden) ms = Math.max(ms, 3500);
  if (!S.conn) ms = 3000;
  pollTimer = setTimeout(poll, ms);
}
function pollNow() { clearTimeout(pollTimer); poll(); }
document.addEventListener('visibilitychange', () => { if (!document.hidden) pollNow(); });

function installKey(i) { return i ? [i.running, i.dep, i.state, i.step, i.progress, i.detail, i.error].join('|') : ''; }
function applyInstall(inst) {
  if (!inst) return;
  const prev = S.install;
  S.install = inst;
  if (inst.running && !prev.running) ensureLogOpen();
  if (installKey(prev) !== installKey(inst)) { updateInstallUI(); updateLogChrome(); }
  if (prev.running && !inst.running) onInstallFinished(inst);
}
async function refreshDeps() {
  try { const d = await api('/api/deps'); S.deps = d.deps; if (d.install) S.install = d.install; } catch (e) { /* offline */ }
}
async function onInstallFinished(inst) {
  const name = depName(inst.dep);
  if (inst.state === 'done') toast(t('toast.install_done', { name }), 'ok');
  else if (inst.state === 'error') toast(t('toast.install_fail', { name }), 'err');
  await refreshDeps();
  if (S.scan && S.view === 'optimize') await rescanQuiet();
  setupAfterInstall(inst);
  renderAll();
}

function phaseKey() {
  const j = S.job;
  if (j && j.running) return 'run:' + j.phase;
  if (j && !S.resultDismissed && ['done', 'cancelled', 'error'].includes(j.phase)) return 'result:' + j.phase;
  return S.scan ? 'review' : 'choose';
}
let renderedKey = '';
function applyJob(job) {
  const prev = S.job;
  S.job = job;
  if (!job) return;
  if (job.running) {
    if (prev && !prev.running) S.resultDismissed = false;
    if (S.view === 'optimize') { if (phaseKey() !== renderedKey) renderOptimize(); else updateRun(); }
    updateLogChrome();
  } else if (prev && prev.running) {
    onJobFinished();
  } else if (!prev && S.view === 'optimize' && phaseKey() !== renderedKey) {
    onJobFinished(true);
  }
}
async function onJobFinished(quiet) {
  try { const r = await api('/api/job?results=1'); S.job = r.job; S.results = r.job ? r.job.results || [] : null; } catch (e) { /* keep the snapshot */ }
  S.resultDismissed = false; S.resultFilter = 'all'; S.resultQuery = '';
  updateLogChrome();
  if (!quiet && S.job) toast(t('toast.finished'), S.job.phase === 'error' ? 'err' : 'ok');
  if (S.view === 'optimize') renderOptimize();
}

// ======================================================================
// global error tracking (shown in the log + written to disk by the server)
// ======================================================================
let lastUiErrorToast = 0;
function reportClientError(message, where) {
  const now = Date.now();
  if (now - lastUiErrorToast > 5000) { lastUiErrorToast = now; toast(t('toast.ui_error'), 'err'); }
  api('/api/clientlog', { level: 'error', message: String(message).slice(0, 500), where: String(where || '').slice(0, 160) }).catch(() => {});
}
window.addEventListener('error', (e) => reportClientError(e.message, `${(e.filename || '').split('/').pop()}:${e.lineno}`));
window.addEventListener('unhandledrejection', (e) => {
  const r = e.reason;
  if (r instanceof NetError || r instanceof ApiError) return;
  reportClientError(r && r.message ? r.message : r, 'promise');
});

// ======================================================================
// overlays (sheets / dialogs)
// ======================================================================
function openOverlay(build, opts = {}) {
  const root = $('#overlay-root');
  const opener = document.activeElement;
  const ov = h('div', { class: 'overlay' });
  const sheet = h('div', { class: 'sheet', role: 'dialog', 'aria-modal': 'true', 'aria-label': opts.label || '', tabindex: '-1' });
  ov.append(sheet);
  root.append(ov);
  $('#main').inert = true;
  let closed = false;
  const ctl = { el: sheet, overlay: ov, close };
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey, true);
    if (!$$('.overlay', root).some((o) => o !== ov && !o.classList.contains('closing'))) $('#main').inert = false;
    ov.classList.add('closing');
    setTimeout(() => ov.remove(), 170);
    if (opener && opener.focus && opener.isConnected) opener.focus({ preventScroll: true });
    if (opts.onClose) opts.onClose();
  }
  function onKey(e) {
    if (ov !== root.lastElementChild) return;
    if (e.key === 'Escape' && !opts.static) { e.preventDefault(); close(); return; }
    if (e.key !== 'Tab') return;
    const f = $$('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea, [tabindex="0"]', sheet).filter((n) => n.offsetParent !== null);
    if (!f.length) { e.preventDefault(); sheet.focus(); return; }
    const first = f[0], last = f[f.length - 1];
    if (!sheet.contains(document.activeElement)) { e.preventDefault(); (e.shiftKey ? last : first).focus(); return; }
    if (e.shiftKey && (document.activeElement === first || document.activeElement === sheet)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
  ov.addEventListener('mousedown', (e) => { if (e.target === ov && !opts.static) close(); });
  document.addEventListener('keydown', onKey, true);
  build(sheet, ctl);
  const af = $('[data-autofocus]', sheet);
  (af || sheet).focus({ preventScroll: true });
  return ctl;
}

function confirmDialog(message, okLabel, danger) {
  return new Promise((resolve) => {
    let answered = false;
    const done = (v) => { if (!answered) { answered = true; resolve(v); } };
    openOverlay((sheet, ctl) => {
      add(sheet, [
        h('div', { class: 'sheet-body' }, h('p', { class: 'break', text: message })),
        h('div', { class: 'sheet-foot' },
          h('button', { class: 'btn', onclick: () => { done(false); ctl.close(); }, text: t('common.cancel') }),
          h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), 'data-autofocus': '1', onclick: () => { done(true); ctl.close(); }, text: okLabel })),
      ]);
    }, { label: message, onClose: () => done(false) });
  });
}

// ======================================================================
// shared widgets
// ======================================================================
const CAT_ICON = { images: 'image', atlas: 'atlas', astc: 'chip', videos: 'video', audio: 'audio', fonts: 'font', scripts: 'code', shaders: 'spark', json: 'braces' };
const catLabel = (id, fallback) => (has('cat.' + id) ? t('cat.' + id) : fallback || id);
const depOf = (id) => S.deps.find((d) => d.id === id);
const requiredMissing = () => S.deps.filter((d) => d.role === 'required' && !d.installed);
const stepLabel = (s) => (s && has('dep.step.' + s) ? t('dep.step.' + s) : '');

function progressBar(pct, cls) {
  const b = h('div', { class: 'bar' + (cls ? ' ' + cls : '') + (pct == null ? ' indeterminate' : ''), role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100' }, h('div', { class: 'fill' }));
  if (pct != null) { b.setAttribute('aria-valuenow', String(Math.round(pct * 100))); b.firstChild.style.width = clamp(pct * 100, 0, 100) + '%'; }
  return b;
}
function setBar(bar, pct) {
  const ind = pct == null;
  bar.classList.toggle('indeterminate', ind);
  const f = bar.firstChild;
  if (ind) { f.style.width = ''; bar.removeAttribute('aria-valuenow'); }
  else { f.style.width = clamp(pct * 100, 0, 100) + '%'; bar.setAttribute('aria-valuenow', String(Math.round(pct * 100))); }
}
function switchEl(checked, onchange, label, disabled) {
  const input = h('input', { type: 'checkbox', role: 'switch', 'aria-label': label, disabled: disabled ? true : null });
  input.checked = !!checked;
  input.addEventListener('change', () => onchange(input.checked, input));
  return h('label', { class: 'switch' }, input, h('span', { class: 'track' }));
}
function copyBtn(text, small = true) {
  return h('button', { class: 'btn ghost icon' + (small ? ' sm' : ''), type: 'button', 'aria-label': t('common.copy'), title: t('common.copy'), onclick: () => copyWithToast(text) }, icon('copy'));
}
function manualBox(hint) {
  if (!hint || (!hint.command && !hint.url)) return null;
  return h('div', { class: 'stack small' },
    h('div', { class: 'muted', text: t('dep.manual') }),
    hint.command ? h('div', { class: 'cmd' }, h('code', { text: hint.command }), copyBtn(hint.command)) : null,
    hint.url ? h('a', { href: hint.url, target: '_blank', rel: 'noopener noreferrer', class: 'break', text: hint.url }) : null);
}

// ---------- dependency install progress (kept live by updateInstallUI) ----------
function fillInstall(el, id) {
  clear(el);
  const I = S.install;
  if (!I || I.dep !== id) { el.hidden = true; return; }
  if (I.running) {
    el.hidden = false; el.className = 'installbox';
    const pct = I.progress == null ? null : I.progress;
    add(el, [
      h('div', { class: 'row' }, h('span', { class: 'spinner' }), h('b', { text: t('dep.installing') }), h('span', { class: 'muted small ellipsis', text: stepLabel(I.step) })),
      progressBar(pct),
      h('div', { class: 'row small muted' }, h('span', { class: 'grow ellipsis', text: I.detail || '' }), pct != null ? h('span', { text: fmtPct(pct * 100) }) : null),
      h('div', { class: 'row' }, h('button', { class: 'btn sm', type: 'button', onclick: cancelInstall, text: t('dep.cancel') })),
    ]);
  } else if (I.state === 'error') {
    el.hidden = false; el.className = 'installbox err';
    add(el, [
      h('div', { class: 'row' }, icon('x'), h('b', { text: t('dep.error') })),
      h('div', { class: 'small break', text: I.error || '' }),
      manualBox(I.hint),
      h('div', { class: 'row' }, h('button', { class: 'btn sm', type: 'button', onclick: () => installDep(id), text: t('common.retry') })),
    ]);
  } else if (I.state === 'cancelled') {
    el.hidden = false; el.className = 'installbox';
    add(el, h('div', { class: 'row muted small' }, icon('info'), t('dep.cancelled')));
  } else el.hidden = true;
}
function installSlot(id) { const el = h('div', { 'data-install-for': id, hidden: true }); fillInstall(el, id); return el; }
let lastInstallRunning = false;
function updateInstallUI() {
  $$('[data-install-for]').forEach((el) => fillInstall(el, el.dataset.installFor));
  if (S.install.running && !lastInstallRunning) {
    const slot = $$('[data-install-for]').find((el) => el.dataset.installFor === S.install.dep && !el.hidden && el.offsetParent !== null);
    if (slot) slot.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  lastInstallRunning = !!S.install.running;
  $$('[data-install-btn]').forEach((b) => { b.disabled = !!S.install.running; });
  updateSetupProgress();
}
async function installDep(id) {
  try {
    const r = await api('/api/deps/install', { id });
    applyInstall(r.install);
    ensureLogOpen();
    pollNow();
  } catch (e) { toast(errText(e) + (e.data && e.data.error && e.code === 'invalid' ? '' : ''), 'err'); if (S.setupQueue) S.setupQueue = null; }
}
async function cancelInstall() { try { await api('/api/deps/cancel', {}); } catch (e) { /* ignore */ } S.setupQueue = null; pollNow(); }
function installButton(dep, primary = true) {
  if (!dep) return null;
  const size = has('dep.size.' + dep.id) ? t('dep.size.' + dep.id) : '';
  if (!dep.can_install) return h('span', { class: 'muted small', text: t('dep.cannot') });
  return h('button', { class: 'btn sm' + (primary ? ' primary' : ''), type: 'button', 'data-install-btn': '1', disabled: S.install.running ? true : null,
    onclick: () => installDep(dep.id) }, icon('download'), size ? t('dep.install_size', { size }) : t('dep.install'));
}

// ======================================================================
// OPTIMIZE view
// ======================================================================
function stepper(idx) {
  const keys = ['step.choose', 'step.review', 'step.optimize', 'step.done'];
  return h('ol', { class: 'steps', 'aria-label': t('step.aria') }, keys.map((k, i) =>
    h('li', { class: i < idx ? 'done' : i === idx ? 'active' : '', 'aria-current': i === idx ? 'step' : null },
      h('span', { class: 'n' }, i < idx ? icon('check') : String(i + 1)), h('span', { text: t(k) }))));
}
function nextHint(text, warn) { return h('div', { class: 'next' + (warn ? ' warn' : '') }, icon(warn ? 'warn' : 'arrow'), h('span', { text })); }

function renderOptimize() {
  const key = phaseKey();
  if (renderedKey && key !== renderedKey && S.view === 'optimize') morph(buildOptimize, 'step');
  else buildOptimize();
}
function buildOptimize() {
  const root = $('#view-optimize');
  const scrollY = window.scrollY;
  clear(root);
  renderedKey = phaseKey();
  const phase = renderedKey.split(':')[0];
  const idx = { choose: 0, review: 1, run: 2, result: 3 }[phase];
  add(root, [
    h('div', { class: 'page-head' }, h('h1', { text: t('opt.title') }), h('p', { text: t('opt.subtitle') })),
    stepper(idx),
    phase !== 'run' ? requiredBanner() : null,
  ]);
  if (phase === 'choose') root.append(chooseCard());
  else if (phase === 'review') root.append(chooseSummary(), reviewCard());
  else if (phase === 'run') root.append(runCard());
  else root.append(...resultCards());
  if (phase === 'run') updateRun();
  const mainCard = $('.card', root), stepsEl = $('.steps', root);       // shared elements: they morph between steps
  if (mainCard) mainCard.style.setProperty('view-transition-name', 'pane');
  if (stepsEl) stepsEl.style.setProperty('view-transition-name', 'steps');
  window.scrollTo({ top: scrollY });
}

function requiredBanner() {
  const miss = requiredMissing();
  if (!miss.length || S.setupOpen) return null;
  return h('div', { class: 'banner warn', role: 'status' }, icon('warn'),
    h('div', { class: 'grow' },
      h('div', { text: miss.map((m) => m.name).join(', ') + ': ' + t('setup.missing') }),
      h('div', { class: 'actions' }, h('button', { class: 'btn sm primary', type: 'button', onclick: () => { S.setupDismissed = false; updateSetupOverlay(true); }, text: t('setup.install_all', { n: miss.length }) }))));
}

// ---------- 1. choose ----------
function chooseCard() {
  const input = h('input', { type: 'text', id: 'path-input', value: S.pathInput || S.prefs.last_path || '', placeholder: t('choose.placeholder'),
    'aria-label': t('choose.label'), autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', inputmode: 'url' });
  input.value = S.pathInput || '';
  input.addEventListener('input', () => { S.pathInput = input.value; if (S.scanError) { S.scanError = null; err.hidden = true; } });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') scanBtn.click(); });
  const scanBtn = h('button', { class: 'btn primary', type: 'button', onclick: () => doScan(input.value, scanBtn) }, icon('search'), t('choose.scan'));
  const err = h('div', { class: 'banner err', role: 'alert', hidden: !S.scanError }, icon('x'), h('div', { class: 'grow break', text: S.scanError ? errText(S.scanError) : '' }));
  const last = S.prefs.last_path && S.prefs.last_path !== S.pathInput
    ? h('div', { class: 'last-used row small' }, h('span', { class: 'muted nowrap', text: t('choose.last') }),
      h('button', { class: 'chipbtn ellipsis', type: 'button', title: S.prefs.last_path, onclick: () => { input.value = S.pathInput = S.prefs.last_path; input.focus(); } }, S.prefs.last_path))
    : null;
  return h('div', { class: 'card hero' },
    h('div', { class: 'card-head' }, h('span', { class: 'num' }, '1'), h('div', null, h('h2', { text: t('choose.title') }), h('p', { class: 'muted small', text: t('choose.hint') }))),
    h('div', { class: 'field' }, input),
    h('div', { class: 'row wrap' }, h('div', { class: 'row wrap' },
      h('button', { class: 'btn', type: 'button', onclick: () => openBrowse(input.value || S.prefs.last_path) }, icon('folder'), t('choose.browse')), scanBtn)),
    last, err, nextHint(t('choose.next')));
}
async function doScan(path, btn) {
  path = (path || '').trim();
  if (!path) { S.scanError = new ApiError(400, { code: 'not_mod' }); renderOptimize(); return; }
  if (btn) btn.classList.add('loading');
  S.scanning = true; S.scanError = null;
  try {
    const r = await api('/api/scan', { path });
    S.scan = r; S.pathInput = r.path; S.prefs.last_path = r.path; S.resultDismissed = true;
    await refreshDeps();
  } catch (e) {
    S.scanError = e; S.scan = null;
  } finally { S.scanning = false; }
  renderOptimize();
  const first = $('#view-optimize .card.review-card'); if (first && S.scan) first.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}
async function rescanQuiet() {
  if (!S.scan) return;
  try { S.scan = await api('/api/scan', { path: S.scan.path }); } catch (e) { /* keep the old scan */ }
}
function resetToChoose() {
  S.scan = null; S.scanError = null; S.resultDismissed = true; S.results = null;
  if (S.job && !S.job.running) api('/api/reset', {}).catch(() => {});
  renderOptimize();
}

// ---------- browse sheet ----------
function openBrowse(startPath) {
  let ctl;
  const body = h('div', { class: 'sheet-body' });
  const foot = h('div', { class: 'sheet-foot' });
  const crumbs = h('div', { class: 'crumbs' });
  const pathbar = h('div', { class: 'pathbar break' });
  async function load(path) {
    body.setAttribute('aria-busy', 'true');
    let data;
    try { data = await api('/api/browse?path=' + encodeURIComponent(path || '')); }
    catch (e) { toast(errText(e), 'err'); body.removeAttribute('aria-busy'); return; }
    body.removeAttribute('aria-busy');
    pathbar.textContent = data.path;
    clear(crumbs);
    for (const r of data.roots) {
      const label = has('root.' + r.key) ? t('root.' + r.key) + (r.key === 'drive' ? ' ' + r.name : '') : r.name;
      crumbs.append(h('button', { class: 'chipbtn', type: 'button', onclick: () => load(r.path) }, label));
    }
    clear(body);
    if (data.parent) body.append(entryBtn('up', '..', t('browse.up'), null, () => load(data.parent)));
    if (!data.entries.length) body.append(h('p', { class: 'muted', text: t('browse.empty') }));
    for (const en of data.entries) {
      body.append(entryBtn(en.type === 'zip' ? 'zip' : 'folder', en.name, null,
        en.type === 'zip' ? h('span', { class: 'badge', text: t('browse.zip') + ' · ' + fmtBytes(en.size) }) : (en.mod ? h('span', { class: 'badge accent', text: t('browse.mod') }) : null),
        () => { if (en.type === 'zip') pick(en.path); else load(en.path); }));
    }
    clear(foot);
    add(foot, [h('button', { class: 'btn', type: 'button', onclick: () => ctl.close() }, t('common.cancel')),
      h('button', { class: 'btn primary', type: 'button', onclick: () => pick(data.path) }, icon('check'), t('browse.use'))]);
    body.scrollTop = 0;
  }
  function entryBtn(ic, name, sub, badge, onclick) {
    return h('button', { class: 'entry', type: 'button', onclick },
      h('span', { class: 'cat-ic' }, icon(ic === 'up' ? 'up' : ic)), h('span', { class: 'grow ellipsis', text: name }), badge);
  }
  function pick(path) { S.pathInput = path; ctl.close(); doScan(path); }
  openOverlay((sheet, c) => {
    ctl = c;
    add(sheet, [h('div', { class: 'sheet-head' }, h('h2', { class: 'grow', text: t('browse.title') }),
      h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': t('common.close'), onclick: () => c.close() }, icon('x'))),
      pathbar, crumbs, body, foot]);
    load(startPath || '');
  }, { label: t('browse.title') });
}

// ---------- 2. review ----------
function chooseSummary() {
  const s = S.scan;
  return h('div', { class: 'card compact' }, h('div', { class: 'row' },
    h('span', { class: 'cat-ic' }, icon(s.kind === 'zip' ? 'zip' : 'folder')),
    h('div', { class: 'grow' }, h('div', { class: 'ellipsis' }, h('b', { text: s.name })), h('div', { class: 'muted small break', text: s.path })),
    h('button', { class: 'btn sm', type: 'button', onclick: resetToChoose }, t('review.change'))));
}
function reviewCard() {
  const s = S.scan;
  const miss = (s.missing || []).filter((m) => { const d = depOf(m.id); return !d || !d.installed; });
  const blocking = miss.some((m) => m.blocking);
  const total = s.categories.reduce((a, c) => a + c.size, 0) || 1;
  const maxSize = Math.max(1, ...s.categories.map((c) => c.size));
  const missByCat = {};
  miss.forEach((m) => m.categories.forEach((c) => { missByCat[c] = m; }));

  const cats = h('div', null, s.categories.map((c) => {
    const m = missByCat[c.id];
    return h('div', { class: 'cat' },
      h('span', { class: 'cat-ic' + (m ? ' warn' : '') }, icon(CAT_ICON[c.id] || 'box')),
      h('div', { class: 'cat-main' },
        h('div', { class: 'cat-title' }, catLabel(c.id, c.label), m ? h('span', { class: 'badge warn' }, icon('warn'), t('missing.needs', { name: m.name })) : null),
        h('div', { class: 'cat-meta', text: t('cat.' + c.id + '.desc') }),
        (() => { const sh = h('div', { class: 'share' }, h('i')); sh.firstChild.style.width = Math.max(3, c.size / maxSize * 100) + '%'; return sh; })()),
      h('div', { class: 'cat-size' }, h('div', { text: fmtBytes(c.size) }), h('div', { class: 'muted small', text: plural('common.files', c.files) })));
  }));

  const banners = miss.map((m) => {
    const d = depOf(m.id);
    const what = m.categories.map((c) => (has('dep.what.' + c) ? t('dep.what.' + c) : c)).join(', ');
    return h('div', { class: 'banner ' + (m.blocking ? 'err' : 'warn') }, icon('warn'),
      h('div', { class: 'grow stack' },
        h('div', { text: m.blocking ? t('missing.block', { name: m.name }) : t('missing.skip', { what, name: m.name }) }),
        h('div', { class: 'actions' }, installButton(d), d && !d.can_install ? manualBox(d.manual) : null),
        installSlot(m.id)));
  });

  const nSet = Object.keys(S.settings.values).filter((k) => !same(S.settings.values[k], (S.settings.schema.find((e) => e.key === k) || {}).default)).length;
  const gpuOk = S.gpu.available;
  const opts = h('div', { class: 'opts' },
    h('div', { class: 'opt' }, h('div', { class: 'grow' }, h('div', { class: 't', text: t('opt.backup.label') }), h('div', { class: 'muted small', text: t('opt.backup.desc') })),
      switchEl(S.prefs.backup, (v) => { S.prefs.backup = v; savePref({ backup: v }); renderOptimize(); }, t('opt.backup.label'))),
    s.categories.some((c) => c.id === 'videos')
      ? h('div', { class: 'opt' }, h('div', { class: 'grow' }, h('div', { class: 't', text: t('opt.gpu.label') }), h('div', { class: 'muted small', text: gpuOk ? t('opt.gpu.desc') : t('opt.gpu.none') })),
        switchEl(gpuOk && S.prefs.gpu, (v) => { S.prefs.gpu = v; savePref({ gpu: v }); }, t('opt.gpu.label'), !gpuOk)) : null,
    h('div', { class: 'opt' }, h('div', { class: 'grow muted small', text: nSet ? t('review.settings.custom', { n: nSet }) : t('review.settings.default') }),
      h('a', { class: 'btn sm', href: '#/settings', text: t('review.settings.change') })));

  const busy = S.install.running;
  const startBtn = h('button', { class: 'btn primary lg', type: 'button', disabled: blocking || busy || S.starting ? true : null, onclick: (e) => startJob(e.currentTarget) },
    icon('play'), t('review.start'));
  const hint = blocking ? nextHint(t('review.next.blocked'), true) : busy ? nextHint(t('review.next.install_busy'), true)
    : S.prefs.backup ? nextHint(t('review.next.backup')) : nextHint(t('review.next.nobackup'), true);

  return h('div', { class: 'card review-card' },
    h('div', { class: 'card-head' }, h('span', { class: 'num' }, '2'),
      h('div', { class: 'grow' }, h('h2', { text: t('review.title') }),
        h('p', { class: 'muted small' }, h('span', { class: 'badge', text: t('review.kind.' + s.kind) }), ' ', t('review.found', { files: plural('common.files', s.total_files), size: fmtBytes(s.total_size) })))),
    ...banners, cats,
    h('div', { class: 'group-title', text: t('review.options') }), opts,
    h('div', { class: 'row' }, startBtn), hint);
}
async function savePref(changes) { try { await api('/api/prefs', { changes }); } catch (e) { toast(errText(e), 'err'); } }
async function startJob(btn) {
  if (btn) btn.classList.add('loading');
  S.starting = true;
  try {
    await api('/api/start', { path: S.scan.path, backup: S.prefs.backup, gpu: S.prefs.gpu && S.gpu.available });
    toast(t('toast.started'), 'info', 1800);
    ensureLogOpen();
    const r = await api('/api/job');
    S.resultDismissed = false; S.results = null; S.cancelling = false;
    S.job = r.job;
    if (S.job && !S.job.running) S.job.running = true;
    renderOptimize();
    pollNow();
  } catch (e) {
    toast(errText(e), 'err');
    if (e instanceof ApiError && e.code === 'missing_required') { await refreshDeps(); await rescanQuiet(); }
  } finally { S.starting = false; renderOptimize(); }
}

// ---------- 3. run ----------
const RUN = {};
function runCard() {
  RUN.title = h('h2', { text: t('run.title') });
  RUN.phase = h('p', { class: 'muted' });
  RUN.pct = h('div', { class: 'big-pct', 'aria-hidden': 'true' });
  RUN.bar = progressBar(0);
  RUN.count = h('span');
  RUN.time = h('span', { class: 'muted' });
  RUN.ok = h('b'); RUN.skip = h('b'); RUN.fail = h('b');
  RUN.saved = h('div', { class: 'muted small' });
  RUN.now = h('ul', { class: 'plain now', 'aria-label': t('run.now') });
  RUN.cancel = h('button', { class: 'btn danger', type: 'button', onclick: cancelJob }, icon('stop'), t('run.cancel'));
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('span', { class: 'num' }, '3'), h('div', { class: 'grow' }, RUN.title, RUN.phase), RUN.cancel),
    h('div', { class: 'row' }, RUN.pct, h('div', { class: 'grow stack' }, RUN.bar,
      h('div', { class: 'row wrap small' }, RUN.count, h('span', { class: 'grow' }), RUN.time))),
    h('div', { class: 'stats' },
      h('div', { class: 'stat ok' }, RUN.ok, h('span', { text: t('run.stat.ok') })),
      h('div', { class: 'stat' }, RUN.skip, h('span', { text: t('run.stat.skip') })),
      h('div', { class: 'stat err' }, RUN.fail, h('span', { text: t('run.stat.fail') }))),
    RUN.saved, RUN.now,
    h('p', { class: 'muted small', text: t('run.note') }));
}
const NOW_ROWS = new Map();         // file name -> its row (updated in place, so bars glide)
function zipEl() {
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'zipic');
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = '<path class="z-body" d="M7 3h7l4 4v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path class="z-fold" d="M14 3v4h4"/>' +
    '<path class="z-zip" d="M11 3.5v14"/><rect class="z-pull" x="9.4" y="14.2" width="3.2" height="4.2" rx="1.1"/>';
  return svg;
}
function nowRow(name) {
  const bar = h('div', { class: 'bar thin fbar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-label': name }, h('div', { class: 'fill' }));
  const pct = h('span', { class: 'fpct' });
  const stage = h('span', { class: 'fstage muted small' });
  const li = h('li', { class: 'frow' }, zipEl(), h('div', { class: 'fmain' }, h('div', { class: 'ftop' }, h('span', { class: 'ellipsis fname', text: name }), pct), bar, stage));
  li._bar = bar; li._pct = pct; li._stage = stage;
  return li;
}
function renderNow(items) {
  const rows = Math.max(1, parseInt(S.prefs.progress_rows, 10) || 4);
  const list = items.map((x) => (typeof x === 'string' ? { name: x, pct: null, stage: '' } : x)).slice(0, rows);
  if (NOW_ROWS.owner !== RUN.now) { NOW_ROWS.clear(); NOW_ROWS.owner = RUN.now; }     // a fresh list for a fresh run
  RUN.now.dataset.pstyle = S.prefs.progress_style || 'both';
  RUN.now.classList.toggle('no-stage', S.prefs.progress_stage === false);
  const keep = new Set(list.map((x) => x.name));
  for (const [name, li] of NOW_ROWS) {
    if (keep.has(name)) continue;
    NOW_ROWS.delete(name);
    li.classList.add('leaving');
    setTimeout(() => li.remove(), 260);
  }
  list.forEach((x, i) => {
    let li = NOW_ROWS.get(x.name);
    if (!li || !li.isConnected) { li = nowRow(x.name); NOW_ROWS.set(x.name, li); RUN.now.append(li); }
    const p = x.pct;
    setBar(li._bar, p);
    li._pct.textContent = p == null ? '' : Math.floor(p * 100) + '%';
    li._stage.textContent = x.stage && x.stage !== 'start' ? t('stage.' + x.stage) : '';
    li.style.order = String(i);
  });
}
function updateRun() {
  const j = S.job;
  if (!j || !RUN.bar || !RUN.bar.isConnected) return;
  const backup = j.phase === 'backup';
  const [bd, bt] = j.backup_progress || [0, 0];
  const total = j.total || 0, done = j.processed || 0;
  const pct = backup ? (bt ? bd / bt : null) : (total ? done / total : (j.phase === 'optimizing' ? 0 : null));
  setBar(RUN.bar, pct);
  RUN.pct.textContent = pct == null ? '…' : fmtPct(pct * 100);
  RUN.phase.textContent = S.cancelling ? t('run.phase.cancelling') : t('run.phase.' + (j.phase === 'starting' ? 'starting' : backup ? 'backup' : 'optimizing'));
  RUN.count.textContent = backup ? (bt ? t('run.backup_progress', { done: fmtNum(bd), total: fmtNum(bt) }) : '') : t('run.progress', { done: fmtNum(done), total: fmtNum(total) });
  const eta = !backup && done > 0 && j.eta > 1 ? ' · ' + t('run.eta', { t: fmtDuration(j.eta) }) : '';
  RUN.time.textContent = t('run.elapsed', { t: fmtDuration(j.elapsed) }) + eta;
  RUN.ok.textContent = fmtNum(j.succeeded || 0); RUN.skip.textContent = fmtNum(j.skipped || 0); RUN.fail.textContent = fmtNum(j.failed || 0);
  RUN.saved.textContent = t('run.saved', { size: fmtBytes(Math.max(0, (j.original || 0) - (j.optimized || 0))) });
  renderNow(j.active_files || []);
  RUN.cancel.disabled = !!S.cancelling;
}
async function cancelJob() {
  S.cancelling = true; updateRun();
  toast(t('toast.cancelling'), 'info', 1600);
  try { await api('/api/cancel', {}); } catch (e) { toast(errText(e), 'err'); S.cancelling = false; }
}

// ---------- 4. result ----------
function resultCards() {
  const j = S.job || {};
  const failed = j.phase === 'error';
  const cancelled = j.phase === 'cancelled';
  const before = j.original || 0, after = j.optimized || 0;
  const saved = Math.max(0, before - after);
  const pct = before ? saved / before * 100 : 0;
  const tone = failed ? 'err' : cancelled || (j.failed || 0) > 0 ? 'warn' : '';
  const title = failed ? t('result.error') : cancelled ? t('result.cancelled') : t('result.done');

  const hero = h('div', { class: 'card result-hero ' + tone },
    h('div', { class: 'check' }, icon(failed ? 'x' : cancelled ? 'stop' : 'check')),
    h('h2', { text: title }),
    failed
      ? h('div', { class: 'stack' }, h('p', { class: 'break', text: errCodeText(j) }), j.error && j.error_code === 'internal' ? h('p', { class: 'muted small break', text: j.error }) : null)
      : [h('div', { class: 'saved-big', text: saved > 0 ? t('result.saved', { size: fmtBytes(saved) }) : t('result.nochange') }),
        saved > 0 ? h('div', { class: 'muted', text: t('result.pct', { pct: fmtPct(pct) }) }) : null,
        h('div', { class: 'before-after muted' }, h('span', { text: fmtBytes(before) }), icon('arrow'), h('b', { text: fmtBytes(after) }), h('span', { class: 'small', text: '· ' + fmtDuration(j.elapsed) }))]);
  const cards = [hero];

  if (cancelled) cards.push(h('div', { class: 'banner info' }, icon('info'), h('div', { class: 'grow', text: t('result.cancelled_note') })));
  if ((j.failed || 0) > 0) cards.push(h('div', { class: 'banner warn' }, icon('warn'), h('div', { class: 'grow' },
    h('div', { text: t('result.failed_note', { n: fmtNum(j.failed) }) }),
    h('div', { class: 'actions' }, h('button', { class: 'btn sm', type: 'button', onclick: () => { setLogFilter('problems'); setLogOpen(true, true); }, text: t('result.show_log') })))));

  const cats = Object.entries(j.by_category || {});
  if (cats.length) {
    const rows = cats.sort((a, b) => (b[1].before - b[1].after) - (a[1].before - a[1].after)).map(([id, v]) => {
      const sv = Math.max(0, v.before - v.after), p = v.before ? sv / v.before : 0;
      return h('tr', null,
        h('td', null, h('div', { class: 'row' }, h('span', { class: 'cat-ic warn-none' }, icon(CAT_ICON[id] || 'box')), h('div', null, h('div', { text: catLabel(id) }),
          (() => { const b = progressBar(p, 'thin'); b.classList.add('mini'); return b; })()))),
        h('td', { class: 'num', text: fmtNum(v.files) }),
        h('td', { class: 'num nowrap' }, h('div', { text: fmtBytes(v.before) + ' → ' + fmtBytes(v.after) }), h('div', { class: 'muted small', text: '−' + fmtPct(p * 100) })));
    });
    cards.push(h('div', { class: 'card' }, h('h2', { text: t('result.by_type') }),
      h('table', { class: 'table' }, h('thead', null, h('tr', null, h('th', { text: t('result.col.type') }), h('th', { class: 'num', text: t('result.col.files') }), h('th', { class: 'num', text: t('result.col.saved') }))), h('tbody', null, rows))));
  }
  if (S.results && S.results.length) cards.push(filesCard());
  if (j.backup_path) cards.push(h('div', { class: 'card compact' }, h('div', { class: 'row' }, h('span', { class: 'cat-ic' }, icon('box')),
    h('div', { class: 'grow' }, h('div', { class: 'muted small', text: t('result.backup') }), h('div', { class: 'break mono small', text: j.backup_path })), copyBtn(j.backup_path))));
  cards.push(h('div', { class: 'card compact' },
    h('div', { class: 'row wrap' },
      h('button', { class: 'btn primary', type: 'button', onclick: resetToChoose }, icon('refresh'), t('result.again')),
      h('button', { class: 'btn', type: 'button', onclick: () => setLogOpen(true, true) }, icon('term'), t('result.show_log'))),
    failed ? null : nextHint(j.backup_path ? t('result.next') : t('result.next.nobackup'))));
  return cards;
}
function errCodeText(j) {
  if (j.error_code && has('err.' + j.error_code)) return t('err.' + j.error_code);
  return j.error || t('err.unknown');
}
let resultLimit = 120;
function filesCard() {
  const card = h('div', { class: 'card' });
  const list = h('div', { class: 'file-list' });
  const tabs = h('div', { class: 'tabs', role: 'group', 'aria-label': t('result.files') });
  const search = h('input', { type: 'search', 'aria-label': t('result.search'), placeholder: t('result.search') });
  search.value = S.resultQuery;
  const map = { ok: 'SUCCESS', skip: 'SKIPPED', fail: 'FAILED' };
  const counts = { all: S.results.length, ok: 0, skip: 0, fail: 0 };
  S.results.forEach((r) => { for (const k of ['ok', 'skip', 'fail']) if (r.status === map[k]) counts[k]++; });
  function draw() {
    clear(list); clear(tabs);
    for (const k of ['all', 'ok', 'skip', 'fail']) {
      if (k === 'fail' && !counts.fail) continue;
      tabs.append(h('button', { type: 'button', 'aria-pressed': String(S.resultFilter === k), onclick: () => { S.resultFilter = k; resultLimit = 120; draw(); } }, `${t('result.f.' + k)} · ${fmtNum(counts[k])}`));
    }
    const q = S.resultQuery.trim().toLowerCase();
    const rows = S.results.filter((r) => (S.resultFilter === 'all' || r.status === map[S.resultFilter]) && (!q || r.name.toLowerCase().includes(q)));
    if (!rows.length) list.append(h('p', { class: 'muted', text: t('result.none') }));
    rows.slice(0, resultLimit).forEach((r) => {
      const sv = r.before - r.after;
      list.append(h('div', { class: 'file' },
        h('span', { class: 'cat-ic' + (r.status === 'FAILED' ? ' warn' : '') }, icon(r.status === 'FAILED' ? 'x' : (CAT_ICON[r.category] || 'box'))),
        h('div', { class: 'fname' }, h('span', { class: 'ellipsis', title: r.name, text: r.name }),
          h('span', { class: 'muted small', text: catLabel(r.category) })),
        h('div', { class: 'fsize' }, r.status === 'SUCCESS' ? [h('div', { text: fmtBytes(r.before) + ' → ' + fmtBytes(r.after) }), h('div', { class: 'badge ok', text: '−' + fmtPct(r.before ? sv / r.before * 100 : 0) })]
          : r.status === 'FAILED' ? h('span', { class: 'badge err', text: t('run.stat.fail') }) : h('span', { class: 'badge', text: t('run.stat.skip') }))));
    });
    if (rows.length > resultLimit) list.append(h('button', { class: 'btn sm', type: 'button', onclick: () => { resultLimit += 200; draw(); }, text: `+${fmtNum(Math.min(200, rows.length - resultLimit))}` }));
  }
  search.addEventListener('input', () => { S.resultQuery = search.value; resultLimit = 120; draw(); });
  draw();
  add(card, [h('h2', { text: t('result.files') }), h('div', { class: 'row wrap', 'data-gap': '1' }, tabs), h('div', { class: 'search' }, icon('search'), search), list,
    h('p', { class: 'muted small', text: t('result.skip_note') })]);
  return card;
}

// ======================================================================
// first-run / required-component setup
// ======================================================================
let setupCtl = null;
const SETUP = { progress: null, primary: null };
function needSetup() { return !!S.app && !S.setupDismissed && (!S.prefs.welcomed || requiredMissing().length > 0); }
function updateSetupOverlay(force) {
  if (force) S.setupDismissed = false;
  const need = needSetup();
  if (need && !setupCtl) {
    openOverlay((sheet, ctl) => { setupCtl = ctl; S.setupOpen = true; buildSetup(sheet); },
      { label: t('setup.title'), static: true, onClose: () => { setupCtl = null; S.setupOpen = false; if (S.view === 'optimize') renderOptimize(); } });
  } else if (!need && setupCtl) setupCtl.close();
  else if (need && setupCtl) buildSetup(setupCtl.el);
}
async function markWelcomed() { S.prefs.welcomed = true; await savePref({ welcomed: true }); }

function setupRow(d, group) {
  const ok = d.installed;
  const spinning = S.install.running && S.install.dep === d.id;
  const stCls = spinning ? 'spin' : ok ? 'ok' : group === 'required' ? 'warn' : '';
  return h('div', { class: 'check-row' },
    h('span', { class: 'dep-status ' + stCls }, spinning ? h('span', { class: 'spinner' }) : icon(ok ? 'check' : group === 'required' ? 'warn' : 'chev')),
    h('div', { class: 'grow' },
      h('div', { class: 'row wrap' }, h('b', { text: d.name }), h('span', { class: 'badge' + (ok ? ' ok' : group === 'required' ? ' warn' : ''), text: ok ? t('dep.installed') + (d.version ? ' · ' + t('dep.version', { v: d.version }) : '') : t('dep.missing') })),
      h('div', { class: 'muted small', text: t('dep.' + d.id + '.desc') }),
      group === 'required' && !ok ? installSlot(d.id) : null));
}
function buildSetup(sheet) {
  clear(sheet);
  const missing = requiredMissing();
  const installable = missing.filter((d) => d.can_install);
  const manualOnly = missing.filter((d) => !d.can_install);
  const req = S.deps.filter((d) => d.role === 'required');
  const cond = S.deps.filter((d) => d.role === 'conditional');
  const opt = S.deps.filter((d) => d.role === 'optional');
  const failedBefore = S.install.state === 'error' && !S.install.running;
  const subtitle = !S.deps.length ? t('setup.checking') : missing.length ? t('setup.missing') : t('setup.ok');

  SETUP.progress = h('div', { class: 'muted small', 'aria-live': 'polite' });
  const skip = h('button', { class: 'btn ghost', type: 'button', onclick: async () => { S.setupDismissed = true; S.setupQueue = null; await markWelcomed(); updateSetupOverlay(); } }, t('setup.skip'));
  SETUP.primary = missing.length
    ? h('button', { class: 'btn primary', type: 'button', 'data-autofocus': '1', disabled: S.install.running || !installable.length ? true : null, onclick: setupInstallAll },
      icon('download'), failedBefore ? t('common.retry') : t('setup.install_all', { n: installable.length }))
    : h('button', { class: 'btn primary', type: 'button', 'data-autofocus': '1', onclick: async () => { await markWelcomed(); updateSetupOverlay(); } }, icon('check'), t('setup.continue'));

  add(sheet, [
    h('div', { class: 'sheet-head' }, logoEl(), h('div', { class: 'grow' }, h('h2', { text: t('setup.title') }), h('div', { class: 'muted small', text: subtitle }))),
    h('div', { class: 'sheet-body' },
      h('div', { class: 'group-title', text: t('setup.required') }), h('div', { class: 'setup-list' }, req.map((d) => setupRow(d, 'required'))),
      manualOnly.map((d) => h('div', { class: 'banner warn' }, icon('warn'), h('div', { class: 'grow stack' }, h('div', { text: t('dep.cannot') }), manualBox(d.manual)))),
      failedBefore ? h('div', { class: 'banner err', role: 'alert' }, icon('x'), h('div', { class: 'grow', text: t('setup.failed') })) : null,
      cond.length ? [h('div', { class: 'group-title', text: t('setup.when_needed') }), h('div', { class: 'setup-list' }, cond.map((d) => setupRow(d, 'conditional')))] : null,
      opt.length ? [h('div', { class: 'group-title', text: t('setup.optional') }), h('div', { class: 'setup-list' }, opt.map((d) => setupRow(d, 'optional'))), h('p', { class: 'muted small', text: t('setup.optional_note') })] : null,
      h('p', { class: 'muted small', text: t('setup.privacy') })),
    h('div', { class: 'sheet-foot' }, SETUP.progress, h('span', { class: 'grow' }), skip, SETUP.primary),
  ]);
  updateSetupProgress();
}
function updateSetupProgress() {
  if (!SETUP.progress || !SETUP.progress.isConnected) return;
  const running = S.install.running && S.setupQueue != null;
  SETUP.progress.textContent = running ? t('setup.installing', { name: depName(S.install.dep), i: S.setupIndex || 1, n: S.setupTotal || 1 }) : '';
  if (SETUP.primary && SETUP.primary.isConnected && requiredMissing().length) SETUP.primary.disabled = !!S.install.running;
}
async function setupInstallAll() {
  S.setupQueue = requiredMissing().filter((d) => d.can_install).map((d) => d.id);
  S.setupTotal = S.setupQueue.length; S.setupIndex = 0;
  await setupNext();
}
async function setupNext() {
  if (!S.setupQueue) return;
  const id = S.setupQueue.shift();
  if (!id) { S.setupQueue = null; await finishSetupIfReady(); return; }
  S.setupIndex += 1;
  await installDep(id);
  updateSetupProgress();
}
async function finishSetupIfReady() {
  await refreshDeps();
  if (!requiredMissing().length) { await markWelcomed(); toast(t('setup.done'), 'ok'); updateSetupOverlay(); renderAll(); }
  else updateSetupOverlay();
}
function setupAfterInstall(inst) {
  if (!S.setupQueue) return;
  if (inst.state === 'done') setupNext(); else { S.setupQueue = null; updateSetupOverlay(); }
}

// ======================================================================
// SETTINGS view
// ======================================================================
let saveTimer = 0, pending = {}, flushing = false, saveState = '';
function setSave(state) { saveState = state; updateSaveInd(); }
function updateSaveInd() {
  const el = $('#saveind');
  if (!el) return;
  clear(el);
  el.className = 'saveind' + (saveState === 'saved' ? ' ok' : saveState === 'err' ? ' err' : '');
  if (saveState === 'saving') add(el, [h('span', { class: 'spinner' }), t('settings.saving')]);
  else if (saveState === 'saved') add(el, [icon('check'), t('settings.saved')]);
  else if (saveState === 'err') add(el, [icon('x'), t('settings.save_failed')]);
}
const schemaOf = (key) => S.settings.schema.find((e) => e.key === key);
const isChanged = (e) => !same(S.settings.values[e.key], e.default);
const countChanged = (group) => S.settings.schema.filter((e) => (!group || e.group === group) && isChanged(e)).length;

function optLabel(key, v) {
  const k = `opt.${key}.${v}`;
  if (has(k)) return t(k);
  if (/_bitrate$/.test(key) && typeof v === 'string') return v.replace(/k$/, ' kbps');
  if (key === 'audio_wav_sample_rate') return fmtNum(v) + ' Hz';
  if (key === 'png_oxipng_level') return String(v);
  return String(v);
}
function paintRange(r) { const min = Number(r.min), max = Number(r.max); r.style.setProperty('--pct', ((Number(r.value) - min) / (max - min) * 100) + '%'); }
function numberControl(e, value, label, commit) {
  const step = e.step || 1;
  const range = h('input', { type: 'range', min: e.min, max: e.max, step, 'aria-label': label });
  const num = h('input', { type: 'number', min: e.min, max: e.max, step, inputmode: 'decimal', 'aria-label': label });
  range.value = num.value = value; paintRange(range);
  range.addEventListener('input', () => { num.value = range.value; paintRange(range); });
  range.addEventListener('change', () => commit(Number(range.value)));
  num.addEventListener('change', () => {
    let v = Number(num.value);
    if (!Number.isFinite(v)) v = Number(range.value);
    v = clamp(v, e.min, e.max);
    if (e.type === 'int') v = Math.round(v / step) * step;
    num.value = range.value = v; paintRange(range); commit(v);
  });
  return h('div', { class: 'ctl' }, range, num, e.unit ? h('span', { class: 'muted small', text: e.unit === 'colors' ? '' : e.unit }) : null);
}
function textControl(e, value, label, commit) {
  const hints = { exclude_patterns: 'data/*.json, scripts/, readme.txt', font_extra_chars: '♪ ♥ ★ ñ' };
  const input = h('input', { type: 'text', maxlength: e.maxlen, 'aria-label': label, placeholder: hints[e.key] || '',
    autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false' });
  input.value = value || '';
  let timer = 0;
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => commit(input.value), 600); });
  input.addEventListener('change', () => { clearTimeout(timer); commit(input.value); });
  return h('div', { class: 'ctl' }, input);
}
function choiceControl(choices, value, label, labelOf, commit) {
  const sel = h('select', { 'aria-label': label }, choices.map((c, i) => h('option', { value: String(i), text: labelOf(c) })));
  sel.value = String(Math.max(0, choices.findIndex((c) => String(c) === String(value))));
  sel.addEventListener('change', () => commit(choices[Number(sel.value)]));
  return sel;
}
function segControl(choices, value, label, labelOf, commit) {
  const seg = h('div', { class: 'seg', role: 'group', 'aria-label': label });
  const paint = (cur) => $$('button', seg).forEach((b, i) => b.setAttribute('aria-pressed', String(String(choices[i]) === String(cur))));
  choices.forEach((c) => seg.append(h('button', { type: 'button', onclick: () => { paint(c); commit(c); } }, labelOf(c))));
  paint(value);
  return seg;
}

function settingRow(e) {
  const key = e.key;
  const label = t(`set.${key}.label`), desc = t(`set.${key}.desc`);
  const value = S.settings.values[key];
  const dep = e.requires ? depOf(e.requires) : null;
  const unmet = !!(dep && !dep.installed);
  const commit = (v) => { queueChange(key, v); refreshRow(row, e); };
  let ctl;
  if (e.type === 'bool') ctl = h('div', { class: 'ctl' }, switchEl(value, (v) => commit(v), label));
  else if (e.type === 'int' || e.type === 'float') ctl = numberControl(e, value, label, commit);
  else if (e.type === 'text') ctl = textControl(e, value, label, commit);
  else ctl = h('div', { class: 'ctl' }, choiceControl(e.choices, value, label, (c) => optLabel(key, c), commit));
  const resetBtn = h('button', { class: 'btn ghost sm icon', type: 'button', 'aria-label': t('settings.reset') + ': ' + label, title: t('settings.reset'),
    onclick: () => { queueChange(key, e.default); row.replaceWith(settingRow(e)); } }, icon('refresh'));
  const dot = h('span', { class: 'mod-dot', title: t('settings.modified') });
  const needs = unmet ? h('span', { class: 'badge warn' }, t('settings.needs', { name: dep.name }),
    dep.can_install ? h('button', { class: 'btn ghost sm', type: 'button', onclick: () => go('settings', 'deps'), text: t('settings.needs_install') }) : null) : null;
  const row = h('div', { class: 'setting' + (unmet ? ' dim' : '') + (e.type === 'text' ? ' wide-ctl' : ''), 'data-key': key, 'data-hay': (label + ' ' + desc + ' ' + key).toLowerCase() },
    h('div', { class: 's-top' }, h('div', { class: 'grow' }, h('div', { class: 's-label' }, label, dot, needs), h('div', { class: 's-desc', text: desc })), resetBtn),
    ctl);
  row._dot = dot; row._reset = resetBtn;
  refreshRow(row, e);
  return row;
}
function refreshRow(row, e) { const ch = isChanged(e); row._dot.hidden = !ch; row._reset.hidden = !ch; updateChangedChips(); }
function updateChangedChips() {
  $$('[data-chg]').forEach((el) => {
    const n = countChanged(el.dataset.chg === 'all' ? null : el.dataset.chg);
    el.hidden = n === 0; el.textContent = t('settings.changed_n', { n });
  });
  $$('[data-reset-group]').forEach((b) => { b.hidden = countChanged(b.dataset.resetGroup === 'all' ? null : b.dataset.resetGroup) === 0; });
}
function queueChange(key, value) {
  S.settings.values[key] = value; pending[key] = value;
  setSave('saving'); clearTimeout(saveTimer); saveTimer = setTimeout(flushSettings, 350);
}
async function flushSettings() {
  if (flushing) return;
  const changes = pending; pending = {};
  if (!Object.keys(changes).length) return;
  flushing = true;
  try {
    const r = await api('/api/settings', { changes });
    S.settings = Object.assign(S.settings, r.settings);
    S.settings.values = Object.assign({}, r.values, pending);
    setSave(Object.keys(pending).length ? 'saving' : 'saved');
  } catch (e) {
    toast(errText(e), 'err'); setSave('err');
    try { const st = await api('/api/state'); S.settings = st.settings; } catch (e2) { /* offline */ }
    pending = {}; if (S.view === 'settings') renderSettings();
  } finally { flushing = false; if (Object.keys(pending).length) flushSettings(); }
}
async function resetSettings(body) {
  try {
    const r = await api('/api/settings/reset', body);
    S.settings = Object.assign(S.settings, r.settings);
    toast(t('toast.settings_reset'), 'ok', 2000); setSave('saved');
    renderSettings(); pollNow();
  } catch (e) { toast(errText(e), 'err'); }
}

function prefRow(entry) {
  const key = entry.key;
  const label = t(`pref.${key}.label`);
  const desc = t(`pref.${key}.desc`, { n: S.app ? S.app.cpus : '' });
  const value = S.prefs[key];
  const commit = (v) => {
    S.prefs[key] = v;
    if (key === 'language') { setLanguage(v); return; }
    if (key === 'theme') morph(applyAppearance, 'theme'); else if (key === 'reduce_motion') applyAppearance();
    savePref({ [key]: v });
    if (key === 'backup' && S.view === 'optimize') renderOptimize();
    if (key.startsWith('progress_')) { NOW_ROWS.clear(); if (RUN.now) clear(RUN.now); updateRun(); }
  };
  let ctl;
  const gpuDisabled = key === 'gpu' && !S.gpu.available;
  if (entry.type === 'bool') ctl = h('div', { class: 'ctl' }, switchEl(value && !gpuDisabled, commit, label, gpuDisabled));
  else if (key === 'language') ctl = choiceControl(Object.keys(window.LANGS), value, label, (c) => `${window.LANGS[c].flag} ${window.LANGS[c].name}`, commit);
  else if (key === 'theme') ctl = segControl(entry.choices, value, label, (c) => t('opt.theme.' + c), commit);
  else if (key === 'workers') ctl = choiceControl(entry.choices, value, label, (c) => (c === 'auto' ? t('common.auto') : String(c)), commit);
  else if (key === 'progress_style') ctl = segControl(entry.choices, value, label, (c) => t('opt.progress_style.' + c), commit);
  else if (key === 'progress_rows') ctl = choiceControl(entry.choices, value, label, (c) => String(c), commit);
  else ctl = choiceControl(entry.choices, value, label, (c) => t(`opt.${key}.${c}`), commit);
  if (ctl.tagName === 'SELECT') ctl = h('div', { class: 'ctl' }, ctl);
  return h('div', { class: 'setting', 'data-hay': (label + ' ' + desc).toLowerCase() },
    h('div', { class: 's-top' }, h('div', { class: 'grow' }, h('div', { class: 's-label', text: label }), h('div', { class: 's-desc', text: gpuDisabled ? t('opt.gpu.none') : desc }))), ctl);
}

function accordion(id, ic, title, desc, content, extra, group) {
  const open = S.openSections.has(id);
  const acc = h('section', { class: 'card acc' + (open ? ' open' : ''), id: 'sec-' + id, 'data-sec': id, 'data-group': group || null });
  const btn = h('button', { class: 'acc-head', type: 'button', 'aria-expanded': String(open), 'aria-controls': 'accb-' + id },
    h('span', { class: 'acc-icon' }, icon(ic)),
    h('span', { class: 'grow' }, h('span', { class: 'row' }, h('span', { class: 'acc-title', text: title }), extra), h('span', { class: 'muted small', text: desc })),
    icon('chev', 'chev'));
  const pad = h('div', { class: 'acc-pad' });
  // the body is filled the first time it is needed (opened, searched, or while idle)
  acc._hydrate = () => {
    if (acc._hydrated) return;
    acc._hydrated = true;
    add(pad, [typeof content === 'function' ? content() : content]);
    if (group) updateChangedChips();
  };
  btn.addEventListener('click', () => {
    const o = !acc.classList.contains('open');
    if (o) acc._hydrate();
    acc.classList.toggle('open', o); btn.setAttribute('aria-expanded', String(o));
    if (o) S.openSections.add(id); else S.openSections.delete(id);
  });
  acc.append(btn, h('div', { class: 'acc-body', id: 'accb-' + id, role: 'region', 'aria-label': title }, h('div', { class: 'acc-inner' }, pad)));
  if (open) acc._hydrate();
  return acc;
}
function hydrateAll() { $$('#view-settings .acc').forEach((a) => a._hydrate && a._hydrate()); }

function depRow(d) {
  const ok = d.installed;
  const spinning = S.install.running && S.install.dep === d.id;
  const stCls = spinning ? 'spin' : ok ? 'ok' : d.role === 'required' ? 'warn' : '';
  return h('div', { class: 'dep', 'data-dep': d.id },
    h('div', { class: 'dep-top' },
      h('span', { class: 'dep-status ' + stCls }, spinning ? h('span', { class: 'spinner' }) : icon(ok ? 'check' : d.role === 'required' ? 'warn' : 'chev')),
      h('div', { class: 'grow stack tight' },
        h('div', { class: 'row wrap' }, h('b', { text: d.name }), h('span', { class: 'badge' + (d.role === 'required' ? ' accent' : ''), text: t('dep.role.' + d.role) }),
          ok ? h('span', { class: 'badge ok', text: t('dep.installed') + (d.version ? ' · ' + t('dep.version', { v: d.version }) : '') }) : h('span', { class: 'badge', text: t('dep.missing') })),
        h('div', { class: 'muted small', text: t('dep.' + d.id + '.desc') }),
        h('div', { class: 'muted small', text: t('dep.used_for', { what: d.needed_for.map((c) => (has('dep.what.' + c) ? t('dep.what.' + c) : c)).join(', ') }) }),
        ok && d.path ? h('div', { class: 'muted small break' }, t('dep.path') + ': ', h('span', { class: 'mono', text: d.path }), ' · ', t('dep.source.' + (d.source || 'system'))) : null,
        ok && !d.path && d.source ? h('div', { class: 'muted small', text: t('dep.source.' + d.source) }) : null,
        !ok && d.role === 'optional' ? h('div', { class: 'muted small', text: t('dep.optional_note') }) : null)),
    !ok ? h('div', { class: 'dep-actions' }, installButton(d, d.role !== 'optional'), !d.can_install ? manualBox(d.manual) : null) : null,
    installSlot(d.id));
}
function depsContent() {
  return h('div', null,
    h('div', { class: 'group-tools' }, h('span', { class: 'muted small', text: S.app ? S.app.app_bin : '' }),
      h('button', { class: 'btn sm', type: 'button', onclick: (ev) => recheckDeps(ev.currentTarget) }, icon('refresh'), t('dep.recheck'))),
    S.deps.map(depRow));
}
async function recheckDeps(btn) {
  if (btn) btn.classList.add('loading');
  try { const r = await api('/api/deps/check', {}); S.deps = r.deps; S.gpu = r.gpu || S.gpu; if (r.install) S.install = r.install; renderAll(); pollNow(); }
  catch (e) { toast(errText(e), 'err'); if (btn) btn.classList.remove('loading'); }
}

function dataContent() {
  const sep = S.app && S.app.platform === 'windows' ? '\\' : '/';
  const kvRow = (k, v) => [h('dt', { text: k }), h('dd', null, h('span', { class: 'mono', text: v }), ' ', copyBtn(v))];
  const row = (labelKey, descKey, btn) => h('div', { class: 'setting' }, h('div', { class: 's-top' }, h('div', { class: 'grow' }, h('div', { class: 's-label', text: t(labelKey) }), h('div', { class: 's-desc', text: t(descKey) }))), h('div', { class: 'ctl' }, btn));
  return h('div', null,
    h('dl', { class: 'kv' }, kvRow(t('data.dir'), S.app.data_dir), kvRow(t('data.bin'), S.app.app_bin), kvRow(t('data.errlog'), S.app.data_dir + sep + 'events.log')),
    row('data.progress.label', 'data.progress.desc', h('button', { class: 'btn', type: 'button', onclick: clearProgressMemory }, icon('trash'), t('data.progress.btn'))),
    row('data.stop.label', 'data.stop.desc', h('button', { class: 'btn danger', type: 'button', onclick: stopApp }, icon('power'), t('data.stop.btn'))));
}
async function clearProgressMemory() {
  try { await api('/api/progress/clear', {}); toast(t('data.progress.done'), 'ok'); } catch (e) { toast(errText(e), 'err'); }
}
let stopped = false;
async function stopApp() {
  if (!(await confirmDialog(t('data.stop.confirm'), t('data.stop.btn'), true))) return;
  try { await api('/api/quit', {}); } catch (e) { /* the server closes the socket */ }
  stopped = true; clearTimeout(pollTimer);
  const b = $('#conn-banner'); clear(b); add(b, [icon('info'), h('div', { class: 'grow', text: t('data.stopped') })]); b.hidden = false;
}

function renderSettings() {
  const root = $('#view-settings');
  const y = window.scrollY;
  clear(root);
  if (!S.app) return;
  stale.settings = false;
  const search = h('input', { type: 'search', 'aria-label': t('settings.search'), placeholder: t('settings.search') });
  search.value = S.settingsQuery;
  search.addEventListener('input', () => { S.settingsQuery = search.value; if (S.settingsQuery.trim()) hydrateAll(); applySettingsFilter(); });
  const ind = h('div', { class: 'saveind', id: 'saveind', role: 'status', 'aria-live': 'polite' });
  const nGroups = S.settings.groups;
  const prefsBody = () => h('div', null, S.settings.prefs_schema.filter((p) => ['language', 'theme', 'reduce_motion', 'backup', 'gpu', 'workers', 'parallel_mode', 'progress_style', 'progress_rows', 'progress_stage'].includes(p.key)).map(prefRow));
  const total = countChanged();
  const compHead = h('div', { class: 'row wrap', id: 'sec-compression' },
    h('h2', { class: 'grow', text: t('sec.compression') }),
    h('span', { class: 'badge accent', 'data-chg': 'all', hidden: total === 0, text: t('settings.changed_n', { n: total }) }),
    h('button', { class: 'btn sm', type: 'button', 'data-reset-group': 'all', hidden: total === 0,
      onclick: async () => { if (await confirmDialog(t('settings.reset_all_confirm'), t('settings.reset_all'))) resetSettings({ all: true }); } }, icon('refresh'), t('settings.reset_all')));
  const groups = nGroups.map((g) => {
    const body = () => {
      const rows = S.settings.schema.filter((e) => e.group === g).map(settingRow);
      const tools = h('div', { class: 'group-tools' }, h('span', { class: 'badge accent', 'data-chg': g, hidden: true }),
        h('button', { class: 'btn sm', type: 'button', 'data-reset-group': g, hidden: true, onclick: () => resetSettings({ group: g }) }, icon('refresh'), t('settings.reset_group')));
      return [tools, rows];
    };
    return accordion('g-' + g, { images: 'image', atlas: 'atlas', astc: 'chip', text: 'code', fonts: 'font', video: 'video', audio: 'audio', advanced: 'shield' }[g] || 'sliders',
      t('group.' + g), t('group.' + g + '.desc'), body, null, g);
  });
  add(root, [
    h('div', { class: 'page-head' }, h('h1', { text: t('settings.title') }), h('p', { text: t('settings.subtitle') })),
    h('div', { class: 'row wrap' }, h('div', { class: 'search grow' }, icon('search'), search), ind),
    accordion('app', 'sliders', t('sec.app'), t('sec.app.desc'), prefsBody, null, 'app'),
    h('div', { class: 'group-head', 'data-compression': '1' }, compHead),
    groups,
    h('p', { class: 'muted', id: 'no-match', hidden: true }),
    accordion('deps', 'box', t('sec.deps'), t('sec.deps.desc'), () => depsContent(), S.deps.some((d) => d.role === 'required' && !d.installed) ? h('span', { class: 'badge warn', text: t('dep.missing') }) : null),
    accordion('data', 'db', t('sec.data'), t('sec.data.desc'), () => dataContent()),
  ]);
  updateSaveInd(); updateChangedChips(); applySettingsFilter();
  if (S.view === 'settings') window.scrollTo({ top: y });
  const todo = $$('.acc', root).filter((a) => !a._hydrated);       // fill the closed ones in the background
  const step = () => { const a = todo.shift(); if (!a) return; if (a.isConnected) a._hydrate(); idle(step); };
  idle(step);
}
function applySettingsFilter() {
  const q = S.settingsQuery.trim().toLowerCase();
  const root = $('#view-settings');
  $$('.setting[data-hay]', root).forEach((r) => r.classList.toggle('hide', !!q && !r.dataset.hay.includes(q)));
  let any = false;
  $$('.acc', root).forEach((acc) => {
    const id = acc.dataset.sec;
    if (acc.dataset.group) {
      const vis = $$('.setting:not(.hide)', acc).length;
      acc.hidden = !!q && !vis;
      if (vis) any = true;
    } else acc.hidden = !!q;
    acc.classList.toggle('open', q && acc.dataset.group ? true : S.openSections.has(id));
    const b = $('.acc-head', acc); if (b) b.setAttribute('aria-expanded', String(acc.classList.contains('open')));
  });
  const head = $('[data-compression]', root); if (head) head.hidden = !!q;
  const nm = $('#no-match', root); nm.hidden = !(q && !any); nm.textContent = t('settings.no_match', { q: S.settingsQuery.trim() });
}

// ======================================================================
// ABOUT view
// ======================================================================
function renderAbout() {
  const root = $('#view-about');
  clear(root);
  if (!S.app) return;
  stale.about = false;
  const A = S.app;
  const techs = [['python', 'Python', 'v' + A.python, true]].concat(['pillow', 'fonttools', 'ffmpeg', 'astcenc', 'oxipng'].map((id) => {
    const d = depOf(id); return [id, d ? d.name : id, d && d.version ? 'v' + d.version : '', d ? d.installed : false];
  }));
  const infoText = [`${A.name} ${A.version}`, `${A.platform}/${A.arch}`, `Python ${A.python}`, `data: ${A.data_dir}`].join('\n');
  add(root, [
    h('div', { class: 'page-head' }, h('h1', { text: t('about.title') })),
    h('div', { class: 'card' },
      h('div', { class: 'about-hero' }, logoEl(),
        h('div', null, h('h2', { text: A.name }), h('div', { class: 'row wrap' }, h('span', { class: 'chip', text: 'v' + A.version })), h('p', { class: 'muted', text: t('about.tagline') }))),
      h('h3', { class: 'group-title', text: t('about.what') }),
      h('ul', { class: 'feat plain' }, ['f1', 'f2', 'f3', 'f4', 'f5'].map((k) => h('li', null, icon('check'), h('span', { text: t('about.' + k) })))),
      h('p', { class: 'muted small', text: t('about.privacy') })),
    h('div', { class: 'card' }, h('h2', { text: t('about.built') }),
      h('div', { class: 'built' }, techs.map(([id, name, ver, ok]) =>
        h('div', null, h('div', { class: 'row wrap' }, h('b', { text: name }), ver ? h('span', { class: 'muted small', text: ver }) : null,
          id === 'python' ? null : h('span', { class: 'badge ' + (ok ? 'ok' : ''), text: ok ? t('dep.installed') : t('dep.missing') })),
          h('div', { class: 'muted small', text: t('about.tech.' + id) }))))),
    h('div', { class: 'card' }, h('h2', { text: t('about.creator') }),
      h('div', { class: 'creator' }, h('div', { class: 'avatar', role: 'img', 'aria-label': 'AkuKatt' }, '🐱'),
        h('div', null, h('b', { text: 'AkuKatt' }), h('div', { class: 'muted small', text: t('about.creator.role') })))),
    accordion('tech', 'term', t('about.tech_info'), '', h('div', null,
      h('dl', { class: 'kv' }, h('dt', { text: t('about.version') }), h('dd', { text: A.version }), h('dt', { text: t('about.platform') }), h('dd', { text: `${A.platform} / ${A.arch}` }),
        h('dt', { text: t('about.python') }), h('dd', { text: A.python }), h('dt', { text: t('data.dir') }), h('dd', { class: 'mono break', text: A.data_dir })),
      h('div', { class: 'row', 'data-gap': '1' }, h('button', { class: 'btn sm', type: 'button', onclick: () => copyWithToast(infoText) }, icon('copy'), t('about.copy_info'))))),
  ]);
}

// ======================================================================
// boot
// ======================================================================
function cachePrefs() { try { localStorage.setItem('fnf.prefs', JSON.stringify({ language: S.prefs.language, theme: S.prefs.theme, reduce_motion: S.prefs.reduce_motion })); } catch (e) { /* ignore */ } }

async function init() {
  const url = new URL(location.href);
  const tok = url.searchParams.get('t');
  if (tok) { try { sessionStorage.setItem('fnf.token', tok); } catch (e) { /* ignore */ } url.searchParams.delete('t'); history.replaceState(null, '', url.pathname + url.hash); }
  try { TOKEN = sessionStorage.getItem('fnf.token') || ''; } catch (e) { TOKEN = tok || ''; }
  try { const c = JSON.parse(localStorage.getItem('fnf.prefs') || '{}'); if (isObj(c)) Object.assign(S.prefs, c); } catch (e) { /* ignore */ }
  LANG = DICT[S.prefs.language] ? S.prefs.language : 'en';
  applyAppearance(); translateStatic(); initLogPanel();
  window.addEventListener('hashchange', () => route(true));
  if (!TOKEN) { showTokenProblem(); return; }
  let st;
  try { st = await api('/api/state'); } catch (e) { route(false); schedule(); return; }
  S.app = st.app; S.deps = st.deps || []; S.install = st.install || S.install; S.gpu = st.gpu || S.gpu;
  S.prefs = Object.assign(S.prefs, st.prefs); S.settings = st.settings; S.job = st.job;
  LANG = DICT[S.prefs.language] ? S.prefs.language : 'en';
  applyAppearance(); translateStatic();
  try { ingestLog((await api('/api/events?since=0')).log); } catch (e) { /* ignore */ }
  if (S.settings.problems && S.settings.problems.length) api('/api/clientlog', { level: 'warn', message: 'Ignored invalid saved settings: ' + S.settings.problems.join('; ') }).catch(() => {});
  route(false);
  updateNavDot(); updateSetupOverlay();
  prewarm();
  if (S.job && !S.job.running && ['done', 'cancelled', 'error'].includes(S.job.phase)) onJobFinished(true);
  else if (S.job && S.job.running) { ensureLogOpen(); renderOptimize(); }
  if (S.install.running) ensureLogOpen();
  schedule();
}
// ======================================================================
// Material ripple (decorative; skipped when motion is reduced)
// ======================================================================
const RIPPLE_HOSTS = '.btn, .entry, .acc-head, .chipbtn, .tabs button, .seg button, .lang button, .log-toggle';
document.addEventListener('pointerdown', (ev) => {
  lastPtr = { x: ev.clientX, y: ev.clientY };
  if (ev.button > 0 || document.documentElement.dataset.motion === 'reduced') return;
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const host = ev.target.closest ? ev.target.closest(RIPPLE_HOSTS) : null;
  if (!host || host.disabled || host.getAttribute('aria-disabled') === 'true' || host.classList.contains('loading')) return;
  const r = host.getBoundingClientRect();
  const x = ev.clientX - r.left, y = ev.clientY - r.top;
  const d = Math.hypot(Math.max(x, r.width - x), Math.max(y, r.height - y)) * 2;
  const s = document.createElement('span');
  s.className = 'ripple';
  s.setAttribute('aria-hidden', 'true');
  s.style.width = s.style.height = d + 'px';
  s.style.left = (x - d / 2) + 'px';
  s.style.top = (y - d / 2) + 'px';
  s.addEventListener('animationend', () => s.remove());
  host.append(s);
}, { passive: true });

init();

})();
