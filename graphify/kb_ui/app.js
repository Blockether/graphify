// graphify knowledge base: map, editors and live updates. No dependencies.
(() => {
'use strict';

// ---------------------------------------------------------------- helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'style') el.style.cssText = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, String(v));
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === undefined || kid === null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const fmt = new Intl.NumberFormat('en-US');
const num = (n) => fmt.format(n || 0).replace(/,/g, '\u2009');
const plural = (n, one, many) => `${num(n)} ${n === 1 ? one : (many || one + 's')}`;
function when(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d)) return String(ts);
  return d.toISOString().slice(0, 16).replace('T', ' ');
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function bytes(n) { if (n < 1024) return `${n} B`; if (n < 1048576) return `${(n / 1024).toFixed(1)} kB`; return `${(n / 1048576).toFixed(1)} MB`; }
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

// ------------------------------------------------------------------ state
const embedded = document.getElementById('kb-data');
const STATIC = embedded ? JSON.parse(embedded.textContent) : null;
const S = {
  readOnly: !!STATIC,
  view: null, overview: null, onto: null, report: null, docs: null, history: null, stats: null,
  route: { tab: 'graph', arg: '', q: {} },
  version: 0,
};
if (S.readOnly) document.body.dataset.readonly = 'true';
// A server started with --read-only says so in /api/health. Ask at start and
// after each reconnect, and hide every write control as in the static export.
async function syncReadOnly() {
  if (STATIC) return;
  let ro = false;
  try { ro = !!(await api('GET', '/api/health')).read_only; } catch (e) { return; }
  if (ro === S.readOnly) return;
  S.readOnly = ro;
  if (ro) document.body.dataset.readonly = 'true'; else delete document.body.dataset.readonly;
  if (S.route && $('#app').childElementCount) rerender();
}

// ------------------------------------------------------------------- api
class ApiError extends Error { constructor(status, message) { super(message); this.status = status; } }
async function api(method, path, body, opts = {}) {
  if (STATIC) throw new ApiError(0, 'This is a read-only copy.');
  const headers = { 'X-Graphify-KB': '1', ...(opts.headers || {}) };
  let payload = body;
  if (body !== undefined && !(body instanceof Blob) && !(body instanceof ArrayBuffer)) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let res;
  // A network error has only the browser's words ("Failed to fetch"): say what happened.
  try { res = await fetch(path, { method, headers, body: payload, credentials: 'same-origin' }); }
  catch { throw new ApiError(0, 'The server did not answer. Check that it runs, then try again.'); }
  if (res.status === 401) { showSignIn(); throw new ApiError(401, 'A token is required.'); }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!res.ok) throw new ApiError(res.status, (data && data.error) || res.statusText);
  return withChanges(data, res.headers.get(CHANGE_HEADER));
}
const enc = encodeURIComponent;
// A write response names its history entries in this header. The UI undoes
// exactly those entries, never "the latest change", which can be another
// person's or an agent's write.
const CHANGE_HEADER = 'X-Graphify-KB-Change';
const CHANGES = Symbol('changes');
function withChanges(data, header) {
  const seqs = (header || '').split(',').map((s) => Number(s.trim())).filter((n) => Number.isInteger(n) && n > 0);
  if (seqs.length && data && typeof data === 'object') Object.defineProperty(data, CHANGES, { value: seqs });
  return data;
}
const changesOf = (r) => (r && r[CHANGES]) || [];

// ------------------------------------------------------------------ toasts
// The timer pauses while the pointer or the focus is on the toast. A toast
// with an action stays 10 s, counted after the running refresh ends.
function toast(message, opts = {}) {
  const box = $('#toasts');
  const el = h('div', { class: 'toast', dataset: { kind: opts.kind || 'info' } }, h('p', { text: message }));
  let left = opts.ms || (opts.action ? 10000 : 8000), timer = null, started = 0;
  const close = () => { clearTimeout(timer); timer = null; el.remove(); };
  const run = () => { if (timer || !el.isConnected || el.matches(':hover') || el.contains(document.activeElement)) return; started = Date.now(); timer = setTimeout(close, left); };
  const pause = () => { if (!timer) return; clearTimeout(timer); timer = null; left = Math.max(2000, left - (Date.now() - started)); };
  el.addEventListener('mouseenter', pause);
  el.addEventListener('mouseleave', () => setTimeout(run, 0));
  el.addEventListener('focusin', pause);
  el.addEventListener('focusout', () => setTimeout(run, 0));
  el.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { ev.stopPropagation(); close(); } });
  el.close = close;
  if (opts.action) {
    el.append(h('button', { class: 'quiet', onclick: async () => { close(); await opts.action.run(); } }, opts.action.label));
  }
  el.append(h('button', { class: 'quiet', 'aria-label': 'Dismiss', onclick: close }, 'Dismiss'));
  box.append(el);
  if (opts.action) setTimeout(() => (refreshing || Promise.resolve()).then(run, run), 0); else run();
}
// Escape closes the newest toast when no field or dialog has the focus.
document.addEventListener('keydown', (ev) => {
  if (ev.key !== 'Escape' || ev.defaultPrevented) return;
  const a = document.activeElement;
  if (a && a !== document.body && !a.closest('#toasts')) return;
  const last = $('#toasts') && $('#toasts').lastElementChild;
  if (last && last.close) last.close();
});
function fail(err) { toast(err && err.message ? err.message : String(err), { kind: 'error' }); }
async function undoLatest() {
  if (S.readOnly) return;
  try { const r = await api('POST', '/api/undo', {}); toast(`Undid: ${r.summary}`); await refreshAll(); } catch (e) { fail(e); }
}
// Undo the given history entries, newest first. A 409 names the later changes
// that touched the same records; stop there and show it.
async function undoSeqs(seqs) {
  let done = 0;
  try {
    for (const seq of [...seqs].sort((a, b) => b - a)) { const r = await api('POST', '/api/undo', { seq }); done += 1; if (seqs.length === 1) toast(`Undid: ${r.summary}`); }
    if (seqs.length > 1) toast(`Undid ${plural(done, 'change')}.`);
  } catch (e) { fail(e); }
  if (done) await refreshAll();
}
// The message after a change. Undo is offered only when the server named the
// history entries of this change.
function undoable(message, ...results) {
  const seqs = results.flat().flatMap(changesOf);
  toast(message, seqs.length ? { action: { label: 'Undo', run: () => undoSeqs(seqs) } } : {});
}

// ------------------------------------------------------------------ router
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, query] = raw.split('?');
  const parts = (path || 'graph').split('/');
  const q = {};
  new URLSearchParams(query || '').forEach((v, k) => { q[k] = v; });
  return { tab: parts[0] || 'graph', arg: decodeURIComponent(parts.slice(1).join('/')), q };
}
function setRoute(tab, arg = '', q = {}, replace = false) {
  const qs = new URLSearchParams(Object.entries(q).filter(([, v]) => v !== '' && v !== undefined && v !== null)).toString();
  const hash = `#/${tab}${arg ? '/' + enc(arg) : ''}${qs ? '?' + qs : ''}`;
  if (hash === location.hash) return;
  if (replace) history.replaceState(null, '', hash); else location.hash = hash;
  if (replace) route();
}
function patchQuery(changes) {
  const q = { ...S.route.q, ...changes };
  setRoute(S.route.tab, S.route.arg, q, true);
}

// ---------------------------------------------------------------- loading
async function loadView() {
  if (STATIC) { S.view = STATIC.view; return; }
  S.view = await api('GET', '/api/graph');
}
async function loadOverview() {
  try { S.overview = await api('GET', '/api/overview'); } catch { S.overview = null; }
}
async function loadOnto() { S.onto = STATIC ? STATIC.ontology : await api('GET', '/api/ontology'); }
async function loadReport() { S.report = STATIC ? STATIC.report : await api('GET', '/api/validate?limit=2000'); }
async function loadDocs() { S.docs = STATIC ? STATIC.documents : (await api('GET', '/api/documents')).documents; }
async function loadHistory() { S.history = STATIC ? [] : (await api('GET', '/api/history?limit=200')).history; }
let refreshing = null;
async function refreshAll() {
  const p = refreshNow(); refreshing = p;
  try { await p; } finally { if (refreshing === p) refreshing = null; }
}
async function refreshNow() {
  await Promise.all([loadView(), loadOnto()]);
  S.report = null; S.docs = null; S.history = null;
  graph.load(S.view);
  updateCounts();
  rerender();
}
function updateCounts() {
  if (!S.view) return;
  $('#count-nodes').textContent = num(S.view.nodes.length);
  $('#count-edges').textContent = num(S.view.edges.length);
}

// ------------------------------------------------------------- palettes
const CATS = Array.from({ length: 12 }, (_, i) => `--cat-${i + 1}`);
let catColors = [];
function palette() {
  if (!catColors.length) catColors = CATS.map(cssVar);
  return catColors;
}
// Classes with a fixed, quiet look. --cat-12 is the grey that class colours never reuse.
const QUIET_CLASSES = { 'code.unresolved': { label: 'Unresolved', color: '--cat-12' } };
function classColor(name) {
  const spec = S.view && S.view.classes ? S.view.classes[name] : null;
  if (spec) return spec;
  if (QUIET_CLASSES[name]) return cssVar(QUIET_CLASSES[name].color);
  const order = graph.classNames;
  const i = order.indexOf(name);
  const p = palette();
  return p[(i < 0 ? hashStr(name) : i) % (p.length - 1)];
}
// Draw timings for the browser checks; only with ?debug=1 in the URL.
const DEBUG = /[?&]debug=1/.test(location.search) ? (window.__kbDebug = { draws: [] }) : null;
// Long namespace and path names keep their last, most specific segments.
function shortName(text, max) {
  if (text.length <= max) return text;
  const cut = text.slice(text.length - max + 1), at = cut.search(/[./\\]/);
  if (/[./\\]/.test(text) && at >= 0 && at < max / 2) return '\u2026' + cut.slice(at + 1);
  return text.slice(0, max - 1) + '\u2026';
}
function hashStr(s) { let x = 0; for (let i = 0; i < s.length; i++) x = (x * 31 + s.charCodeAt(i)) | 0; return Math.abs(x); }

// ---------------------------------------------------------- layout kernel
// ForceAtlas2 (Jacomy et al. 2014): repulsion kr(deg+1)(deg+1)/d through a
// Barnes-Hut quadtree with theta 1.2, kr = 2, LinLog attraction log(1 + d)
// (Noack 2007) and the adaptive local speed of the reference code. With
// anchors, strong gravity pulls every node to its community centre and a
// clamp keeps it inside the community disc: the force-directed group-in-a-box
// layout (Chaturvedi et al. 2014). The same function runs in a Web Worker or,
// as a fallback, on the main thread.
function layoutKernel() {
  let n = 0, m = 0, x, y, fx, fy, ox, oy, es, et, ew = null, mass, size = null, pinned;
  let kr = 2, kg = 1, strong = false, linlog = true, adjust = false, theta2 = 1.44, jitter = 1;
  let speed = 1, efficiency = 1, iter = 0, maxIter = 500, calm = 0, last = Infinity;
  let ci = null, ax = null, ay = null, ar = null, kgc = null, C = 0, cStart = null, cList = null;
  let cap = 0, qx, qy, qs, qm, qmx, qmy, qb, qc, qn = 0, stack;
  function grow(need) {
    if (need <= cap) return;
    cap = Math.max(need, cap * 2, 1024);
    const f = (a, T) => { const b = new T(cap); if (a) b.set(a.subarray(0, Math.min(a.length, cap))); return b; };
    qx = f(qx, Float64Array); qy = f(qy, Float64Array); qs = f(qs, Float64Array);
    qm = f(qm, Float64Array); qmx = f(qmx, Float64Array); qmy = f(qmy, Float64Array);
    qb = f(qb, Int32Array); qc = f(qc, Int32Array);
  }
  function cell(c, x0, y0, s) { qx[c] = x0; qy[c] = y0; qs[c] = s; qm[c] = 0; qmx[c] = 0; qmy[c] = 0; qb[c] = -1; qc[c] = -1; }
  function child(c, px, py) { const half = qs[c] / 2; return qc[c] + (px >= qx[c] + half ? 1 : 0) + (py >= qy[c] + half ? 2 : 0); }
  function split(c) {
    const base = qn; qn += 4; grow(qn);
    const half = qs[c] / 2;
    for (let q = 0; q < 4; q++) cell(base + q, qx[c] + (q & 1) * half, qy[c] + (q >> 1) * half, half);
    qc[c] = base;
  }
  function insert(i, root) {
    let c = root, depth = 0;
    for (;;) {
      qm[c] += mass[i]; qmx[c] += x[i] * mass[i]; qmy[c] += y[i] * mass[i];
      if (qc[c] === -1) {
        if (qb[c] === -1) { qb[c] = i; return; }
        if (depth > 40) return;
        const j = qb[c]; qb[c] = -1; split(c);
        const cj = child(c, x[j], y[j]);
        qm[cj] += mass[j]; qmx[cj] += x[j] * mass[j]; qmy[cj] += y[j] * mass[j]; qb[cj] = j;
      }
      c = child(c, x[i], y[i]); depth++;
    }
  }
  function tree(root, list, from, to) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (let k = from; k < to; k++) { const i = list ? list[k] : k; if (x[i] < x0) x0 = x[i]; if (x[i] > x1) x1 = x[i]; if (y[i] < y0) y0 = y[i]; if (y[i] > y1) y1 = y[i]; }
    if (from === to) { cell(root, 0, 0, 1); return; }
    cell(root, x0, y0, Math.max(x1 - x0, y1 - y0) + 1);
    for (let k = from; k < to; k++) insert(list ? list[k] : k, root);
  }
  // With communities, nodes repel only inside their own community: the
  // discs never overlap, so forces between them would only push every
  // community to its rim. One quadtree per community.
  function build() {
    if (!cList) { qn = 1; grow(4 * n + 8); tree(0, null, 0, n); return; }
    qn = C; grow(4 * n + C + 8);
    for (let c = 0; c < C; c++) tree(c, cList, cStart[c], cStart[c + 1]);
  }
  // Two nodes on the same point get a fixed, index-based push, so a run
  // never depends on Math.random().
  function nudge(i) { const a = (i * 2.399963) % (Math.PI * 2); return [Math.cos(a) * 0.01, Math.sin(a) * 0.01]; }
  function repulse() {
    for (let i = 0; i < n; i++) {
      let sp = 0; stack[sp++] = cList ? ci[i] : 0;
      let rx = 0, ry = 0;
      const xi = x[i], yi = y[i], mi = mass[i];
      while (sp > 0) {
        const c = stack[--sp];
        const mc = qm[c];
        if (mc === 0) continue;
        const leaf = qc[c] === -1;
        if (leaf && qb[c] === i) continue;
        let dx, dy;
        if (leaf) { const j = qb[c]; dx = xi - x[j]; dy = yi - y[j]; } else { dx = xi - qmx[c] / mc; dy = yi - qmy[c] / mc; }
        let d2 = dx * dx + dy * dy;
        if (leaf || qs[c] * qs[c] < theta2 * d2) {
          if (d2 < 1e-4) { [dx, dy] = nudge(i); d2 = dx * dx + dy * dy; }
          let f;
          if (adjust && leaf && size) {
            // Anti-collision: distance between the borders of the two discs.
            const d = Math.sqrt(d2), gap = d - size[i] - size[qb[c]];
            f = gap > 0 ? kr * mi * mc / (gap * gap) * (gap / d) : 100 * kr * mi * mc / d;
          } else f = kr * mi * mc / d2;
          rx += dx * f; ry += dy * f;
        } else {
          const b = qc[c];
          stack[sp++] = b; stack[sp++] = b + 1; stack[sp++] = b + 2; stack[sp++] = b + 3;
        }
      }
      fx[i] += rx; fy[i] += ry;
    }
  }
  function attract() {
    for (let e = 0; e < m; e++) {
      const s = es[e], t = et[e];
      const dx = x[t] - x[s], dy = y[t] - y[s];
      const d = Math.sqrt(dx * dx + dy * dy);
      if (d < 1e-9) continue;
      const w = ew ? ew[e] : 1;
      const f = linlog ? w * Math.log(1 + d) / d : w;
      fx[s] += dx * f; fy[s] += dy * f;
      fx[t] -= dx * f; fy[t] -= dy * f;
    }
  }
  function gravity() {
    for (let i = 0; i < n; i++) {
      const gx = ci ? ax[ci[i]] : 0, gy = ci ? ay[ci[i]] : 0;
      const dx = gx - x[i], dy = gy - y[i], d = Math.sqrt(dx * dx + dy * dy);
      if (d < 1e-9) continue;
      const k = kgc ? kgc[ci[i]] : kg;
      const f = strong ? k * mass[i] : k * mass[i] / d;
      fx[i] += dx * f; fy[i] += dy * f;
    }
  }
  // FA2 adaptive speed (Jacomy et al. 2014, section "Adaptive speed").
  function move() {
    let swing = 0, traction = 0;
    for (let i = 0; i < n; i++) {
      if (pinned[i]) continue;
      const sx = fx[i] - ox[i], sy = fy[i] - oy[i], tx = fx[i] + ox[i], ty = fy[i] + oy[i];
      swing += mass[i] * Math.sqrt(sx * sx + sy * sy);
      traction += mass[i] * 0.5 * Math.sqrt(tx * tx + ty * ty);
    }
    if (traction > 0 && swing > 0) {
      const optimal = 0.05 * Math.sqrt(n);
      let jt = jitter * Math.max(Math.sqrt(optimal), Math.min(10, optimal * traction / (n * n)));
      if (swing / traction > 2) { if (efficiency > 0.05) efficiency *= 0.5; jt = Math.max(jt, jitter); }
      const target = jt * efficiency * traction / swing;
      if (swing > jt * traction) { if (efficiency > 0.05) efficiency *= 0.7; } else if (speed < 1000) efficiency *= 1.3;
      speed += Math.min(target - speed, 0.5 * speed);
    }
    let moved = 0;
    for (let i = 0; i < n; i++) {
      if (pinned[i]) continue;
      const sx = fx[i] - ox[i], sy = fy[i] - oy[i];
      const sw = mass[i] * Math.sqrt(sx * sx + sy * sy);
      let factor = speed / (1 + Math.sqrt(speed * sw));
      const df = Math.sqrt(fx[i] * fx[i] + fy[i] * fy[i]);
      if (adjust) factor = df > 0 ? Math.min(0.1 * factor * df, 10) / df : 0;
      let nx = x[i] + fx[i] * factor, ny = y[i] + fy[i] * factor;
      if (ci && ar) {
        // Group-in-a-box: the node stays inside its community disc.
        const c = ci[i], rx = nx - ax[c], ry = ny - ay[c], r = Math.sqrt(rx * rx + ry * ry), lim = ar[c];
        if (r > lim) { nx = ax[c] + rx / r * lim; ny = ay[c] + ry / r * lim; }
      }
      moved += Math.abs(nx - x[i]) + Math.abs(ny - y[i]);
      x[i] = nx; y[i] = ny;
    }
    return n ? moved / n : 0;
  }
  function step() {
    iter++;
    if (n === 0) return 0;
    const t = fx; fx = ox; ox = t; const u = fy; fy = oy; oy = u;
    fx.fill(0); fy.fill(0);
    build(); repulse(); attract(); gravity();
    const moved = move();
    // Stop after 20 calm steps: the mean move stays under 0.2 % of the start.
    if (last === Infinity) last = moved;
    calm = moved < last * 0.002 ? calm + 1 : 0;
    return moved;
  }
  return {
    init(d) {
      n = d.n; m = d.m; x = d.x; y = d.y; es = d.es; et = d.et; ew = d.ew || null; mass = d.mass; pinned = d.pinned;
      size = d.size || null; kr = d.kr || 2; kg = d.kg === undefined ? 1 : d.kg; strong = !!d.strong;
      linlog = d.linlog !== false; adjust = !!d.adjust; theta2 = (d.theta || 1.2) ** 2; jitter = d.jitter || 1;
      ci = d.ci || null; ax = d.ax || null; ay = d.ay || null; ar = d.ar || null; kgc = d.kgc || null;
      C = ci ? d.C : 0; cStart = null; cList = null;
      if (C) {
        cStart = new Int32Array(C + 1); cList = new Int32Array(n);
        for (let i = 0; i < n; i++) cStart[ci[i] + 1]++;
        for (let c = 0; c < C; c++) cStart[c + 1] += cStart[c];
        const fill = cStart.slice(0, C);
        for (let i = 0; i < n; i++) cList[fill[ci[i]]++] = i;
      }
      fx = new Float64Array(n); fy = new Float64Array(n); ox = new Float64Array(n); oy = new Float64Array(n);
      stack = new Int32Array(4 * 64 + 16);
      speed = 1; efficiency = 1; iter = 0; calm = 0; last = Infinity; maxIter = d.maxIter || 500;
    },
    step,
    done() { return iter >= maxIter || calm >= 20; },
    reheat(steps) { maxIter = iter + (steps || 120); calm = 0; last = Infinity; speed = Math.max(speed, 1); },
    pin(i, px, py) { pinned[i] = 1; x[i] = px; y[i] = py; },
    unpin(i) { pinned[i] = 0; },
    set(opts) { if ('adjust' in opts) adjust = !!opts.adjust; if (opts.maxIter) maxIter = iter + opts.maxIter; calm = 0; last = Infinity; },
    positions() { return { x, y }; },
  };
}

// Seed positions come from a hash of the node id, so the same graph gives the
// same map on every load, whatever the row order.
function hash32(s) {
  let h1 = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) { h1 ^= s.charCodeAt(i); h1 = Math.imul(h1, 0x01000193); }
  h1 ^= h1 >>> 16; h1 = Math.imul(h1, 0x85ebca6b); h1 ^= h1 >>> 13; h1 = Math.imul(h1, 0xc2b2ae35); h1 ^= h1 >>> 16;
  return h1 >>> 0;
}
function seedAngle(id) { return (hash32(String(id)) / 4294967296) * Math.PI * 2; }

// Meta-layout of the communities (group-in-a-box, Chaturvedi et al. 2014):
// FA2 with LinLog on the community graph, link weights as edge weights; then
// scale to the disc sizes and remove every overlap. Input: ids, sizes and
// links [a, b, weight] by index. Output: centre and radius of each disc.
function metaLayout(ids, sizes, links, unit) {
  const C = ids.length;
  const R = new Float64Array(C), x = new Float64Array(C), y = new Float64Array(C), deg = new Float64Array(C);
  for (let c = 0; c < C; c++) R[c] = unit * Math.sqrt(sizes[c]) + unit;
  for (const [a, b] of links) { deg[a]++; deg[b]++; }
  // Seed on a spiral by size; the hash turns each seed a little.
  const order = Array.from({ length: C }, (_, c) => c).sort((a, b) => sizes[b] - sizes[a] || (ids[a] < ids[b] ? -1 : 1));
  order.forEach((c, k) => { const a = k * 2.399963 + seedAngle(ids[c]) * 0.05, d = Math.sqrt(k + 1) * 4; x[c] = Math.cos(a) * d; y[c] = Math.sin(a) * d; });
  if (C > 1) {
    const es = new Int32Array(links.length), et = new Int32Array(links.length), ew = new Float64Array(links.length);
    links.forEach(([a, b, w], j) => { es[j] = a; et[j] = b; ew[j] = w; });
    const mass = Float64Array.from(deg, (d) => d + 1);
    const K = layoutKernel();
    K.init({ n: C, m: links.length, x, y, es, et, ew, mass, pinned: new Uint8Array(C), kr: 2, kg: 1, theta: 1.2, linlog: true, maxIter: 400 });
    while (!K.done()) K.step();
    // Scale so a typical linked pair just touches, then push discs apart.
    const ratio = [];
    for (const [a, b] of links) { const d = Math.hypot(x[a] - x[b], y[a] - y[b]); if (d > 1e-9) ratio.push((R[a] + R[b]) / d); }
    ratio.sort((p, q) => p - q);
    const f = ratio.length ? ratio[Math.floor(ratio.length / 2)] : unit * 4;
    for (let c = 0; c < C; c++) { x[c] *= f; y[c] *= f; }
    separate(x, y, R, C, unit);
    // FA2 gravity leaves groups without links on a far ring. Pull all discs
    // to the centre and remove the overlaps again, a few times (packing).
    for (let round = 0; round < 24; round++) {
      let mx = 0, my = 0, wt = 0;
      for (let c = 0; c < C; c++) { const w = R[c] * R[c]; mx += x[c] * w; my += y[c] * w; wt += w; }
      mx /= wt; my /= wt;
      for (let c = 0; c < C; c++) { x[c] = mx + (x[c] - mx) * 0.85; y[c] = my + (y[c] - my) * 0.85; }
      separate(x, y, R, C, unit);
    }
  }
  return { x, y, r: R };
}

// Push overlapping discs apart: sweep on the left edges, so only pairs whose
// x-intervals overlap are compared.
function separate(px, py, R, count, gap) {
  const idx = Array.from({ length: count }, (_, i) => i);
  for (let round = 0; round < 200; round++) {
    idx.sort((a, b) => (px[a] - R[a]) - (px[b] - R[b]));
    let moved = false;
    for (let a = 0; a < count; a++) {
      const i = idx[a];
      for (let b = a + 1; b < count; b++) {
        const j = idx[b];
        if (px[j] - R[j] > px[i] + R[i] + gap) break;
        let dx = px[j] - px[i], dy = py[j] - py[i];
        let d = Math.sqrt(dx * dx + dy * dy);
        const need = R[i] + R[j] + gap;
        if (d >= need) continue;
        if (d < 1e-6) { const ang = ((i * 7 + j) * 2.399963) % (Math.PI * 2); dx = Math.cos(ang); dy = Math.sin(ang); d = 1; }
        const push = need - d, wi = R[j] * R[j] / (R[i] * R[i] + R[j] * R[j]);
        px[i] -= dx / d * push * wi; py[i] -= dy / d * push * wi;
        px[j] += dx / d * push * (1 - wi); py[j] += dy / d * push * (1 - wi);
        moved = true;
      }
    }
    if (!moved) break;
  }
}

function workerMain() {
  const K = layoutKernel();
  let running = false, timer = null;
  function post() {
    const p = K.positions();
    self.postMessage({ type: 'pos', x: Float32Array.from(p.x), y: Float32Array.from(p.y), done: K.done() });
  }
  function loop() {
    const t0 = Date.now();
    while (!K.done() && Date.now() - t0 < 30) K.step();
    post();
    if (K.done()) { running = false; return; }
    timer = setTimeout(loop, 0);
  }
  function start() { if (!running) { running = true; timer = setTimeout(loop, 0); } }
  self.onmessage = (ev) => {
    const d = ev.data;
    if (d.type === 'init') { clearTimeout(timer); running = false; K.init(d); post(); start(); }
    else if (d.type === 'pin') { K.pin(d.i, d.x, d.y); K.reheat(d.steps); start(); }
    else if (d.type === 'unpin') { K.unpin(d.i); }
    else if (d.type === 'reheat') { K.reheat(d.steps); start(); }
    else if (d.type === 'stop') { clearTimeout(timer); running = false; }
  };
}

// ------------------------------------------------------------ graph model
// World scale: a community disc has the radius UNIT * (sqrt(size) + 1).
const UNIT = 10;
// Community centres by community key. A reload after an edit reuses them, so
// the map keeps its shape (the mental map) while the graph changes.
const anchorsByKey = new Map();

const graph = {
  n: 0, m: 0, ids: [], labels: [], cls: null, comm: null, deg: null, kb: null,
  index: new Map(), es: null, et: null, er: null, relations: [], classNames: [], classOf: [],
  x: null, y: null, pinned: null, visible: null, C: 0, metaOnly: false,
  adjStart: null, adjList: null, order: null, worker: null, kernel: null, running: false,
  leafParent: null, leafCount: null, leafStart: null, leafList: null, core: null, kIdx: null,
  expanded: -1, showLeaves: false, folded: 0,
  // First paint: the community meta-graph of /api/overview, before the nodes
  // arrive (overview first, Shneiderman 1996).
  loadOverview(ov) {
    if (this.n || !ov || !Array.isArray(ov.communities)) return;
    const rows = ov.communities, pos = new Map(rows.map((c, j) => [String(c.id), j]));
    const links = [];
    for (const l of ov.links || []) {
      const a = pos.get(String(l.source)), b = pos.get(String(l.target));
      if (a !== undefined && b !== undefined && a !== b) links.push([a, b, l.weight || 1]);
    }
    const major = (c) => { let best = '', most = -1; for (const [k, v] of Object.entries(c.classes || {})) if (v > most) { best = k; most = v; } return best; };
    this.metaOnly = true;
    this.setCommunities(rows.map((c) => c.id), rows.map((c) => c.size || 1), rows.map((c) => c.name || ''), rows.map(major), links);
    this.classNames = Array.from(new Set(this.cClass)); this.classOf = this.classNames;
    this.cVisible = Float64Array.from(this.cSize);
    this.cTop = rows.map((c) => c.top || []);
    map.dirty = true; map.fitPending = true;
  },
  load(view) {
    const old = new Map();
    if (this.x && this.n) for (let i = 0; i < this.n; i++) old.set(this.ids[i], [this.x[i], this.y[i]]);
    // Community indices change on each load; the open one is kept by its key.
    const openKey = map.openComm >= 0 && this.cKey ? String(this.cKey[map.openComm]) : null;
    this.metaOnly = false; this.cTop = null;
    const rows = view.nodes;
    const n = rows.length;
    this.n = n; this.ids = new Array(n); this.labels = new Array(n);
    this.cls = new Int32Array(n); this.comm = new Int32Array(n); this.deg = new Int32Array(n); this.kb = new Uint8Array(n);
    this.index = new Map();
    const classIndex = new Map();
    const classCount = [];
    for (let i = 0; i < n; i++) {
      const r = rows[i];
      this.ids[i] = r[0]; this.labels[i] = r[1];
      let c = classIndex.get(r[2]);
      if (c === undefined) { c = classIndex.size; classIndex.set(r[2], c); classCount.push(0); }
      classCount[c]++;
      this.cls[i] = c; this.comm[i] = r[3]; this.deg[i] = r[4]; this.kb[i] = r[5];
      this.index.set(r[0], i);
    }
    // Classes sorted by size keep stable colours for the big ones.
    const names = Array.from(classIndex.keys());
    const sorted = names.map((nm, i) => [nm, classCount[i]]).sort((a, b) => b[1] - a[1]).map((p) => p[0]);
    this.classNames = sorted;
    this.classCounts = new Map(names.map((nm, i) => [nm, classCount[i]]));
    this.classOf = names;
    const m = view.edges.length;
    this.m = m; this.es = new Int32Array(m); this.et = new Int32Array(m); this.er = new Int32Array(m);
    this.relations = view.relations;
    for (let e = 0; e < m; e++) { const r = view.edges[e]; this.es[e] = r[0]; this.et[e] = r[1]; this.er[e] = r[2]; }
    // adjacency (both directions)
    const cnt = new Int32Array(n + 1);
    for (let e = 0; e < m; e++) { cnt[this.es[e] + 1]++; cnt[this.et[e] + 1]++; }
    for (let i = 0; i < n; i++) cnt[i + 1] += cnt[i];
    const list = new Int32Array(2 * m); const fill = cnt.slice();
    for (let e = 0; e < m; e++) { list[fill[this.es[e]]++] = e; list[fill[this.et[e]]++] = e; }
    this.adjStart = cnt; this.adjList = list;
    this.order = Array.from({ length: n }, (_, i) => i).sort((a, b) => this.deg[b] - this.deg[a]);
    this.pinned = new Uint8Array(n);
    this.visible = new Uint8Array(n).fill(1);
    this.foldLeaves();
    this.buildCommunities(view.communities || {});
    map.openComm = openKey === null ? -1 : this.communityOf(openKey); map.hoverComm = -1;
    // Core nodes start at their old place, else at a hashed point in their disc.
    this.x = new Float64Array(n); this.y = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const c = this.ci[i], p = old.get(this.ids[i]);
      if (p && Math.hypot(p[0] - this.cx[c], p[1] - this.cy[c]) <= this.cr[c]) { this.x[i] = p[0]; this.y[i] = p[1]; continue; }
      const h = hash32(this.ids[i]), a = (h & 0xffff) / 65536 * Math.PI * 2, r = this.cr[c] * 0.7 * Math.sqrt((h >>> 16) / 65536);
      this.x[i] = this.cx[c] + Math.cos(a) * r; this.y[i] = this.cy[c] + Math.sin(a) * r;
    }
    if (this.expanded >= 0) this.expanded = -1;
    this.placeLeaves();
    this.applyFilter();
    this.startLayout(old.size ? 160 : 400);
    map.invalidateGrid();
    map.dirty = true;
    if (!old.size) map.fitPending = true;
  },
  // Fold degree-1 leaves into their only neighbour (aggregation, Elmqvist
  // and Fekete 2010). Leaves skip the force layout and sit on a sunflower
  // around their parent. They show when the parent is selected.
  foldLeaves() {
    const n = this.n, parent = new Int32Array(n).fill(-1), count = new Int32Array(n), ids = this.ids;
    const degree = (i) => this.adjStart[i + 1] - this.adjStart[i];
    for (let i = 0; i < n; i++) {
      if (degree(i) !== 1 || this.comm[i] < 0) continue;
      const e = this.adjList[this.adjStart[i]], o = this.es[e] === i ? this.et[e] : this.es[e];
      if (o === i || this.comm[o] !== this.comm[i]) continue;
      // Of two linked leaves, the one with the larger id folds into the other.
      if (degree(o) === 1 && ids[i] < ids[o]) continue;
      parent[i] = o; count[o]++;
    }
    const start = new Int32Array(n + 1);
    for (let i = 0; i < n; i++) start[i + 1] = start[i] + count[i];
    const list = new Int32Array(start[n]), at = start.slice(0, n);
    for (let i = 0; i < n; i++) if (parent[i] >= 0) list[at[parent[i]]++] = i;
    const core = [];
    this.kIdx = new Int32Array(n).fill(-1);
    for (let i = 0; i < n; i++) if (parent[i] < 0) { this.kIdx[i] = core.length; core.push(i); }
    this.leafParent = parent; this.leafCount = count; this.leafStart = start; this.leafList = list;
    this.core = Int32Array.from(core); this.folded = n - core.length;
  },
  // Dense community numbers, member lists, the edges inside each community
  // and the summed edges between communities. The overview draws these.
  buildCommunities(named) {
    const n = this.n, dense = new Map();
    this.ci = new Int32Array(n);
    for (let i = 0; i < n; i++) {
      // Nodes without a community each form their own group.
      const key = this.comm[i] >= 0 ? this.comm[i] : -1 - i;
      let c = dense.get(key); if (c === undefined) { c = dense.size; dense.set(key, c); }
      this.ci[i] = c;
    }
    const C = dense.size, keys = new Array(C), sizes = new Array(C).fill(0), hub = new Int32Array(C).fill(-1);
    for (const [key, c] of dense) keys[c] = key;
    const votes = new Map();
    for (let i = 0; i < n; i++) {
      const c = this.ci[i]; sizes[c]++;
      if (hub[c] < 0 || this.deg[i] > this.deg[hub[c]]) hub[c] = i;
      const vk = c * 4096 + this.cls[i]; votes.set(vk, (votes.get(vk) || 0) + 1);
    }
    // The class most members share colours the community in the overview.
    const major = new Array(C).fill(''), best = new Int32Array(C);
    for (const [vk, v] of votes) { const c = Math.floor(vk / 4096); if (v > best[c]) { best[c] = v; major[c] = this.classOf[vk % 4096]; } }
    const agg = new Map();
    this.ew = new Float32Array(this.m);
    const intra = new Int32Array(C + 1), inter = [];
    for (let e = 0; e < this.m; e++) {
      const a = this.ci[this.es[e]], b = this.ci[this.et[e]];
      // Edges between communities pull less: each node stays near its group.
      this.ew[e] = a === b ? 1 : 0.35;
      if (a === b) { intra[a + 1]++; continue; }
      inter.push(e);
      const key = a < b ? a * C + b : b * C + a;
      agg.set(key, (agg.get(key) || 0) + 1);
    }
    for (let c = 0; c < C; c++) intra[c + 1] += intra[c];
    const ceList = new Int32Array(intra[C]), at = intra.slice(0, C);
    for (let e = 0; e < this.m; e++) { const a = this.ci[this.es[e]]; if (a === this.ci[this.et[e]]) ceList[at[a]++] = e; }
    this.ceStart = intra; this.ceList = ceList; this.xEdges = Int32Array.from(inter);
    const mStart = new Int32Array(C + 1);
    for (let i = 0; i < n; i++) mStart[this.ci[i] + 1]++;
    for (let c = 0; c < C; c++) mStart[c + 1] += mStart[c];
    const mList = new Int32Array(n), mAt = mStart.slice(0, C);
    for (const i of this.order) mList[mAt[this.ci[i]]++] = i;
    this.mStart = mStart; this.mList = mList; this.cHub = hub;
    const links = Array.from(agg.entries(), ([key, w]) => [Math.floor(key / C), key % C, w]);
    this.setCommunities(keys, sizes, keys.map((k) => (k >= 0 ? named[String(k)] || '' : '')), major, links);
  },
  setCommunities(keys, sizes, names, classes, links) {
    const C = keys.length;
    this.C = C; this.cKey = keys; this.cSize = Int32Array.from(sizes); this.cName = names; this.cClass = classes;
    if (!this.n) this.cHub = new Int32Array(C).fill(-1);
    const pairs = links.slice().sort((p, q) => q[2] - p[2]);
    this.cEs = Int32Array.from(pairs, (p) => p[0]); this.cEt = Int32Array.from(pairs, (p) => p[1]); this.cEw = Int32Array.from(pairs, (p) => p[2]);
    this.cOrder = Array.from({ length: C }, (_, c) => c).sort((p, q) => this.cSize[q] - this.cSize[p]);
    this.placeAnchors(links);
  },
  placeAnchors(links) {
    const C = this.C, cx = new Float64Array(C), cy = new Float64Array(C), cr = new Float64Array(C);
    const missing = [];
    for (let c = 0; c < C; c++) {
      cr[c] = UNIT * (Math.sqrt(this.cSize[c]) + 1);
      const a = anchorsByKey.get(String(this.cKey[c]));
      if (a) { cx[c] = a[0]; cy[c] = a[1]; } else missing.push(c);
    }
    if (missing.length === C) {
      const L = metaLayout(this.cKey.map(String), this.cSize, links, UNIT);
      cx.set(L.x); cy.set(L.y);
    } else {
      // A new community starts beside its heaviest placed neighbour, else
      // outside the current map.
      const isNew = new Uint8Array(C); for (const c of missing) isNew[c] = 1;
      let reach = 0;
      for (let c = 0; c < C; c++) if (!isNew[c]) reach = Math.max(reach, Math.hypot(cx[c], cy[c]) + cr[c]);
      for (const c of missing) {
        let near = -1, w = 0;
        for (const [a, b, lw] of links) { const o = a === c ? b : b === c ? a : -1; if (o >= 0 && !isNew[o] && lw > w) { near = o; w = lw; } }
        const ang = seedAngle(this.cKey[c]);
        if (near >= 0) { const d = cr[near] + cr[c] + UNIT; cx[c] = cx[near] + Math.cos(ang) * d; cy[c] = cy[near] + Math.sin(ang) * d; }
        else { const d = reach + cr[c] + UNIT; cx[c] = Math.cos(ang) * d; cy[c] = Math.sin(ang) * d; }
      }
      separate(cx, cy, cr, C, UNIT);
    }
    for (let c = 0; c < C; c++) anchorsByKey.set(String(this.cKey[c]), [cx[c], cy[c]]);
    this.cx = cx; this.cy = cy; this.cr = cr;
  },
  communityName(c) {
    const key = this.cKey[c];
    if (this.n && key < 0) return this.labels[-1 - key];
    return this.cName[c] || (this.cHub[c] >= 0 ? this.labels[this.cHub[c]] : `Community ${key}`);
  },
  communityOf(key) { for (let c = 0; c < this.C; c++) if (String(this.cKey[c]) === String(key)) return c; return -1; },
  // Leaves sit on a sunflower around their parent, inside the community disc.
  placeLeaves() {
    const x = this.x, y = this.y;
    for (let o = 0; o < this.n; o++) {
      const a = this.leafStart[o], b = this.leafStart[o + 1];
      if (a === b) continue;
      const base = seedAngle(this.ids[o]), c = this.ci[o], lim = this.cr[c];
      for (let j = a; j < b; j++) {
        const i = this.leafList[j], k = j - a, r = 5 + 3.2 * Math.sqrt(k), ang = base + k * 2.399963;
        let px = x[o] + Math.cos(ang) * r, py = y[o] + Math.sin(ang) * r;
        const dx = px - this.cx[c], dy = py - this.cy[c], d = Math.hypot(dx, dy);
        if (d > lim) { px = this.cx[c] + dx / d * lim; py = this.cy[c] + dy / d * lim; }
        x[i] = px; y[i] = py;
      }
    }
  },
  startLayout(maxIter) {
    const core = this.core, K = core.length, C = this.C;
    const x = new Float64Array(K), y = new Float64Array(K), mass = new Float64Array(K), pinned = new Uint8Array(K), ci = new Int32Array(K);
    const total = new Float64Array(C);
    for (let k = 0; k < K; k++) {
      const i = core[k];
      x[k] = this.x[i]; y[k] = this.y[i]; mass[k] = 1 + this.deg[i]; pinned[k] = this.pinned[i]; ci[k] = this.ci[i]; total[ci[k]] += mass[k];
    }
    // Strong gravity per community: with repulsion kr * M / r and gravity
    // kg * r the core settles at r = sqrt(kr * M / kg). Pick kg so that r is
    // 70 % of the disc radius; the leaves use the rest.
    const kgc = new Float64Array(C);
    for (let c = 0; c < C; c++) { const r = this.cr[c] * 0.7; kgc[c] = total[c] ? 2 * total[c] / (r * r) : 1; }
    const es = [], et = [], ew = [];
    for (let e = 0; e < this.m; e++) {
      const a = this.kIdx[this.es[e]], b = this.kIdx[this.et[e]];
      if (a < 0 || b < 0 || a === b) continue;
      es.push(a); et.push(b); ew.push(this.ew[e]);
    }
    const payload = {
      type: 'init', n: K, m: es.length, x, y, es: Int32Array.from(es), et: Int32Array.from(et), ew: Float64Array.from(ew), mass, pinned,
      kr: 2, theta: 1.2, linlog: true, strong: true, kgc, ci, C, ax: this.cx, ay: this.cy, ar: this.cr, maxIter,
    };
    this.running = true; ui.layoutState();
    if (!this.worker && !this.kernel) {
      try {
        const src = `${layoutKernel.toString()}\n(${workerMain.toString()})();`;
        this.worker = new Worker(URL.createObjectURL(new Blob([src], { type: 'text/javascript' })));
        this.worker.onmessage = (ev) => this.onPositions(ev.data);
        this.worker.onerror = () => { this.worker = null; this.kernel = layoutKernel(); this.startLayout(maxIter); };
      } catch { this.kernel = layoutKernel(); }
    }
    if (this.worker) this.worker.postMessage(payload);
    else { this.kernel.init(payload); this.mainLoop(); }
  },
  mainLoop() {
    const K = this.kernel; if (!K) return;
    const t0 = performance.now();
    while (!K.done() && performance.now() - t0 < 12) K.step();
    const p = K.positions();
    this.onPositions({ x: p.x, y: p.y, done: K.done() });
    if (!K.done() && this.running) requestAnimationFrame(() => this.mainLoop());
  },
  onPositions(d) {
    if (d.x.length !== this.core.length) return;
    const core = this.core;
    for (let k = 0; k < core.length; k++) { this.x[core[k]] = d.x[k]; this.y[core[k]] = d.y[k]; }
    this.placeLeaves();
    if (d.done) { this.running = false; ui.layoutState(); }
    map.invalidateGrid(); map.dirty = true;
    if (map.followSet) map.fitSet(map.followSet); else if (map.fitPending) map.fit(false);
  },
  stopLayout() { this.running = false; if (this.worker) this.worker.postMessage({ type: 'stop' }); ui.layoutState(); },
  reheat() { this.running = true; ui.layoutState(); if (this.worker) this.worker.postMessage({ type: 'reheat', steps: 150 }); else if (this.kernel) { this.kernel.reheat(150); this.mainLoop(); } },
  // Only core nodes can be dragged; a leaf follows its parent.
  pin(i, px, py) {
    const k = this.kIdx[i]; if (k < 0) return false;
    const c = this.ci[i], dx = px - this.cx[c], dy = py - this.cy[c], d = Math.hypot(dx, dy);
    if (d > this.cr[c]) { px = this.cx[c] + dx / d * this.cr[c]; py = this.cy[c] + dy / d * this.cr[c]; }
    this.pinned[i] = 1; this.x[i] = px; this.y[i] = py;
    if (this.worker) this.worker.postMessage({ type: 'pin', i: k, x: px, y: py, steps: 60 });
    else if (this.kernel) { this.kernel.pin(k, px, py); }
    this.placeLeaves();
    return true;
  },
  neighbors(i) {
    const out = [];
    for (let p = this.adjStart[i]; p < this.adjStart[i + 1]; p++) {
      const e = this.adjList[p]; out.push([e, this.es[e] === i ? this.et[e] : this.es[e], this.es[e] === i]);
    }
    return out;
  },
  // Show the folded leaves of one parent (details on demand).
  expand(i) {
    const p = i >= 0 && this.leafParent[i] >= 0 ? this.leafParent[i] : i;
    if (p === this.expanded) return;
    this.expanded = p >= 0 && this.leafCount[p] ? p : -1;
    this.applyFilter();
  },
  applyFilter() {
    if (!this.n) return;
    const hidden = ui.hiddenClasses, C = this.C, cv = new Float64Array(C);
    for (let i = 0; i < this.n; i++) {
      const shown = hidden.has(this.classOf[this.cls[i]]) ? 0 : 1;
      if (shown) cv[this.ci[i]]++;
      const p = this.leafParent[i];
      this.visible[i] = shown && (p < 0 || this.showLeaves || p === this.expanded) ? 1 : 0;
    }
    this.cVisible = cv;
    map.dirty = true; map.invalidateGrid();
  },
};

// ------------------------------------------------------------- map render
const map = {
  canvas: null, ctx: null, w: 0, h: 0, dpr: 1, scale: 1, tx: 0, ty: 0,
  dirty: true, fitPending: false, followSet: null, grid: null, gridAt: 0, cell: 40,
  hover: -1, selected: -1, focusSet: null, colorBy: 'class', labels: 'auto', detail: 'auto', drag: null,
  hoverComm: -1, openComm: -1, overview: false, noteText: '', reach: 10, frameMs: 0,
  colors: {},
  mount(container) {
    this.canvas = h('canvas', { tabindex: '0', role: 'img', 'aria-label': 'Graph map. Use the search box to find a node, then the inspector to read and edit it.' });
    container.append(this.canvas);
    this.ctx = this.canvas.getContext('2d');
    if (DEBUG) { DEBUG.map = this; DEBUG.graph = graph; }
    this.colors = {
      edge: cssVar('--color-rule-2'), accent: cssVar('--color-accent'), ink: cssVar('--color-ink'),
      ink2: cssVar('--color-ink-2'), muted: cssVar('--color-muted'), paper: cssVar('--color-paper-3'), cool: cssVar('--color-cool'),
    };
    this.fonts = { body: cssVar('--font-body') || 'sans-serif', mono: cssVar('--font-outlier') || 'monospace' };
    const ro = new ResizeObserver(() => this.resize());
    ro.observe(container);
    this.resize();
    this.bind();
    if (!this.loopOn) { this.loopOn = true; const tick = () => { if (this.dirty && this.canvas && this.canvas.isConnected) this.draw(); requestAnimationFrame(tick); }; requestAnimationFrame(tick); }
  },
  resize() {
    const r = this.canvas.parentElement.getBoundingClientRect();
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.w = Math.max(1, r.width); this.h = Math.max(1, r.height);
    this.canvas.width = Math.round(this.w * this.dpr); this.canvas.height = Math.round(this.h * this.dpr);
    this.dirty = true;
  },
  toScreen(x, y) { return [x * this.scale + this.tx, y * this.scale + this.ty]; },
  toWorld(sx, sy) { return [(sx - this.tx) / this.scale, (sy - this.ty) / this.scale]; },
  radius(i) { return 2.2 + Math.sqrt(graph.deg[i]) * 0.9; },
  // Map height that the bottom sheet leaves free on narrow screens. A selection
  // opens the sheet, so framing calls pass open = true. 0.62 matches its max-height.
  viewH(open) {
    const pane = $('.workbench .pane.right');
    if (!pane || getComputedStyle(pane).position !== 'absolute') return this.h;
    const shown = open === undefined ? $('.workbench').dataset.sheet === 'open' : open;
    return shown ? Math.max(120, this.h - Math.min(this.h * 0.62, 560)) : this.h;
  },
  fit() {
    const g = graph; if (!g.C) return;
    this.followSet = null;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (let c = 0; c < g.C; c++) {
      if (g.cVisible && !g.cVisible[c]) continue;
      x0 = Math.min(x0, g.cx[c] - g.cr[c]); x1 = Math.max(x1, g.cx[c] + g.cr[c]);
      y0 = Math.min(y0, g.cy[c] - g.cr[c]); y1 = Math.max(y1, g.cy[c] + g.cr[c]);
    }
    if (!isFinite(x0)) return;
    const pad = this.w < 600 ? 12 : 32, bw = Math.max(x1 - x0, 1), bh = Math.max(y1 - y0, 1);
    const H = this.viewH();
    const s = Math.min((this.w - pad * 2) / bw, (H - pad * 2) / bh, 4);
    this.scale = s; this.tx = this.w / 2 - (x0 + bw / 2) * s; this.ty = H / 2 - (y0 + bh / 2) * s;
    this.dirty = true;
  },
  // Frame one community disc (drill-down).
  fitCommunity(c) {
    const g = graph, pad = this.w < 600 ? 12 : 40;
    const H = this.viewH(true);
    const s = Math.min((this.w - pad * 2) / (2 * g.cr[c]), (H - pad * 2) / (2 * g.cr[c]), 6);
    this.scale = s; this.tx = this.w / 2 - g.cx[c] * s; this.ty = H / 2 - g.cy[c] * s;
    this.fitPending = false; this.followSet = null; this.dirty = true;
  },
  centerOn(i, zoom) {
    if (i < 0) return;
    if (zoom) this.scale = Math.max(this.scale, zoom);
    this.tx = this.w / 2 - graph.x[i] * this.scale; this.ty = this.viewH(true) / 2 - graph.y[i] * this.scale;
    this.fitPending = false; this.followSet = null; this.dirty = true;
  },
  fitSet(set, maxScale = 3) {
    // Frame a node with its neighbours, so a selection shows its context.
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const i of set) { x0 = Math.min(x0, graph.x[i]); y0 = Math.min(y0, graph.y[i]); x1 = Math.max(x1, graph.x[i]); y1 = Math.max(y1, graph.y[i]); }
    if (!isFinite(x0)) return;
    const pad = this.w < 600 ? 24 : 60, bw = Math.max(x1 - x0, 40), bh = Math.max(y1 - y0, 40), H = this.viewH(true);
    this.scale = Math.min((this.w - pad * 2) / bw, (H - pad * 2) / bh, maxScale);
    this.tx = this.w / 2 - (x0 + (x1 - x0) / 2) * this.scale; this.ty = H / 2 - (y0 + (y1 - y0) / 2) * this.scale;
    // Keep the frame while the layout still moves, until the user moves the camera.
    this.fitPending = false; this.followSet = set; this.dirty = true;
  },
  // Positions or visibility changed: the grid and the edge layer are stale.
  invalidateGrid() { this.grid = null; this.edgeVersion = (this.edgeVersion || 0) + 1; },
  buildGrid() {
    const g = graph, c = this.cell, grid = new Map();
    for (let i = 0; i < g.n; i++) {
      if (!g.visible[i]) continue;
      const key = Math.floor(g.x[i] / c) + ',' + Math.floor(g.y[i] / c);
      let b = grid.get(key); if (!b) grid.set(key, (b = [])); b.push(i);
    }
    this.grid = grid;
  },
  pick(sx, sy, only) {
    if (!this.grid) this.buildGrid();
    const [wx, wy] = this.toWorld(sx, sy);
    const px = this.reach || 10;
    if (only) {
      let best = -1, bestD = Infinity;
      for (const i of only) {
        if (!graph.visible[i]) continue;
        const d = Math.hypot(graph.x[i] * this.scale + this.tx - sx, graph.y[i] * this.scale + this.ty - sy);
        if (d < px && d < bestD) { best = i; bestD = d; }
      }
      return best;
    }
    const c = this.cell, gx = Math.floor(wx / c), gy = Math.floor(wy / c);
    const reach = Math.max(1, Math.ceil((px / this.scale) / c));
    let best = -1, bestD = Infinity;
    for (let ix = gx - reach; ix <= gx + reach; ix++) for (let iy = gy - reach; iy <= gy + reach; iy++) {
      const b = this.grid.get(ix + ',' + iy); if (!b) continue;
      for (const i of b) {
        const dx = graph.x[i] - wx, dy = graph.y[i] - wy, d = Math.sqrt(dx * dx + dy * dy);
        const r = Math.max(this.radius(i), (px - 4) / this.scale);
        if (d < r + 4 / this.scale && d < bestD) { best = i; bestD = d; }
      }
    }
    return best;
  },
  nodeColor(i) {
    if (this.colorBy === 'community') {
      const p = palette(); const c = graph.comm[i];
      return c < 0 ? p[p.length - 1] : p[c % (p.length - 1)];
    }
    return classColor(graph.classOf[graph.cls[i]]);
  },
  select(i, opts = {}) {
    this.selected = i;
    graph.expand(i);
    this.focusSet = null; this.followSet = null;
    if (i >= 0) {
      const set = new Set([i]);
      for (const [, o] of graph.neighbors(i)) set.add(o);
      this.focusSet = set;
      if (opts.center) this.fitSet(set);
    }
    this.dirty = true;
  },
  draw() {
    const t0 = performance.now();
    this.dirty = false;
    const ctx = this.ctx, g = graph, dpr = this.dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);
    if (!g.C) return;
    this.overview = this.useOverview();
    this.setNote();
    this.blitEdges();
    if (this.overview) this.drawOverview();
    else {
      const comms = this.commsInView();
      this.drawMembers(comms, true);
      this.drawLabels(comms);
    }
    this.frameMs = performance.now() - t0;
    if (DEBUG) DEBUG.draws.push([this.frameMs, this.overview ? 1 : 0]);
  },
  // Communities whose disc touches the viewport. Node detail draws only
  // these, so the cost follows what is on screen, not the graph size.
  commsInView(m = 0) {
    const g = graph, s = this.scale, out = [];
    for (let c = 0; c < g.C; c++) {
      if (g.cVisible && !g.cVisible[c]) continue;
      const x = g.cx[c] * s + this.tx, y = g.cy[c] * s + this.ty, r = g.cr[c] * s + 30;
      if (x + r < -m || x - r > this.w + m || y + r < -m || y - r > this.h + m) continue;
      out.push(c);
    }
    return out;
  },
  nodeR(i) { return Math.max(1.4, Math.min(this.radius(i) * Math.sqrt(this.scale), 28)); },
  inView(i) {
    const sx = graph.x[i] * this.scale + this.tx, sy = graph.y[i] * this.scale + this.ty;
    return sx > -50 && sx < this.w + 50 && sy > -50 && sy < this.h + 50;
  },
  // Cached edge layer. Long, semi-transparent edges cost the most to
  // rasterise, so they go to an offscreen canvas a margin larger than the
  // view. While the camera moves, one drawImage moves and scales it. The
  // layer is drawn again when the camera rests for 150 ms, when the edges
  // change (edgeVersion) or when the zoom moves more than 4 times from it.
  blitEdges() {
    const L = this.layer || (this.layer = { canvas: document.createElement('canvas'), key: '', s: 1, tx: 0, ty: 0, m: 0 });
    const now = performance.now(), cam = `${this.scale}|${this.tx}|${this.ty}`;
    if (cam !== this.lastCam) { this.lastCam = cam; this.camAt = now; }
    const key = `${this.overview ? 'o' : 'd'}|${this.edgeVersion || 0}|${this.selected}|${this.openComm}|${this.w}x${this.h}@${this.dpr}`;
    const k = this.scale / L.s, fresh = L.s === this.scale && L.tx === this.tx && L.ty === this.ty;
    if (key !== L.key || k < 0.25 || k > 4 || (!fresh && now - this.camAt >= 150)) this.renderEdges(L, key);
    else if (!fresh && !this.settleTimer) this.settleTimer = setTimeout(() => { this.settleTimer = 0; this.dirty = true; }, 160);
    const f = this.scale / L.s, m = L.m;
    this.ctx.drawImage(L.canvas, (-m - L.tx) * f + this.tx, (-m - L.ty) * f + this.ty, (this.w + 2 * m) * f, (this.h + 2 * m) * f);
  },
  renderEdges(L, key) {
    const W = this.w, H = this.h, dpr = this.dpr, m = Math.round(Math.min(W, H) * 0.25);
    const cw = Math.round((W + 2 * m) * dpr), ch = Math.round((H + 2 * m) * dpr);
    if (L.canvas.width !== cw || L.canvas.height !== ch) { L.canvas.width = cw; L.canvas.height = ch; }
    const ctx = L.canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, cw, ch);
    ctx.setTransform(dpr, 0, 0, dpr, m * dpr, m * dpr);
    if (this.overview) this.strokeOverviewEdges(ctx, -m, -m, W + m, H + m);
    else this.strokeMemberEdges(ctx, this.commsInView(m), true, -m, -m, W + m, H + m);
    Object.assign(L, { key, s: this.scale, tx: this.tx, ty: this.ty, m });
  },
  strokeMemberEdges(ctx, comms, between, x0, y0, x1, y1) {
    const g = graph, s = this.scale, tx = this.tx, ty = this.ty, focus = this.focusSet, vis = g.cVisible;
    const inside = (x, y) => x >= x0 && x <= x1 && y >= y0 && y <= y1;
    const line = (e, near) => {
      const a = g.es[e], b = g.et[e];
      if (!g.visible[a] || !g.visible[b]) return;
      const ax = g.x[a] * s + tx, ay = g.y[a] * s + ty, bx = g.x[b] * s + tx, by = g.y[b] * s + ty;
      if (near ? !(inside(ax, ay) && inside(bx, by)) : (ax < x0 && bx < x0) || (ax > x1 && bx > x1) || (ay < y0 && by < y0) || (ay > y1 && by > y1)) return;
      ctx.moveTo(ax, ay); ctx.lineTo(bx, by);
    };
    // Member edges only for communities in view (Elmqvist and Fekete 2010).
    ctx.lineWidth = s > 1.5 ? 1 : 0.6;
    ctx.strokeStyle = this.colors.edge;
    ctx.globalAlpha = focus ? 0.08 : 0.32;
    ctx.beginPath();
    for (const c of comms) for (let p = g.ceStart[c]; p < g.ceStart[c + 1]; p++) line(g.ceList[p], false);
    ctx.stroke();
    if (between) {
      // Edges between communities one by one when both ends are near the
      // view. The others are summed into one line per community pair.
      const on = new Uint8Array(g.C); for (const c of comms) on[c] = 1;
      ctx.globalAlpha = focus ? 0.05 : 0.16;
      ctx.beginPath();
      for (const e of g.xEdges) if (on[g.ci[g.es[e]]] || on[g.ci[g.et[e]]]) line(e, true);
      ctx.stroke();
      ctx.globalAlpha = focus ? 0.04 : 0.12; ctx.lineWidth = 0.8;
      ctx.beginPath();
      for (let j = 0; j < g.cEs.length; j++) {
        const a = g.cEs[j], b = g.cEt[j];
        if ((!on[a] && !on[b]) || (vis && (!vis[a] || !vis[b]))) continue;
        const ax = g.cx[a] * s + tx, ay = g.cy[a] * s + ty, bx = g.cx[b] * s + tx, by = g.cy[b] * s + ty;
        if (inside(ax, ay) && inside(bx, by)) continue;
        ctx.moveTo(ax, ay); ctx.lineTo(bx, by);
      }
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  },
  drawMembers(comms, between) {
    const ctx = this.ctx, g = graph, s = this.scale, tx = this.tx, ty = this.ty, W = this.w, H = this.h;
    const focus = this.focusSet;
    // At node detail the edges come from the edge layer; a drill-down draws its own.
    if (!between) this.strokeMemberEdges(ctx, comms, false, 0, 0, W, H);
    if (this.selected >= 0) {
      ctx.globalAlpha = 0.9; ctx.strokeStyle = this.colors.accent; ctx.lineWidth = 1.4;
      ctx.beginPath();
      for (const [e] of g.neighbors(this.selected)) {
        const a = g.es[e], b = g.et[e];
        if (!g.visible[a] || !g.visible[b]) continue;
        ctx.moveTo(g.x[a] * s + tx, g.y[a] * s + ty); ctx.lineTo(g.x[b] * s + tx, g.y[b] * s + ty);
      }
      ctx.stroke();
    }
    // nodes, batched by colour
    const buckets = new Map(), kb = [], badges = [];
    for (const c of comms) {
      for (let p = g.mStart[c]; p < g.mStart[c + 1]; p++) {
        const i = g.mList[p];
        if (!g.visible[i] || !this.inView(i)) continue;
        const col = this.nodeColor(i);
        let b = buckets.get(col); if (!b) buckets.set(col, (b = [])); b.push(i);
        if (g.kb[i]) kb.push(i);
        if (g.leafCount[i] && i !== g.expanded && !g.showLeaves) badges.push(i);
      }
    }
    for (const [col, list] of buckets) {
      ctx.fillStyle = col;
      for (const pass of focus ? [0, 1] : [1]) {
        ctx.globalAlpha = focus ? (pass ? 1 : 0.14) : 0.92;
        ctx.beginPath();
        for (const i of list) {
          if (focus && (pass === 1) !== focus.has(i)) continue;
          const r = this.nodeR(i), sx = g.x[i] * s + tx, sy = g.y[i] * s + ty;
          ctx.moveTo(sx + r, sy); ctx.arc(sx, sy, r, 0, Math.PI * 2);
        }
        ctx.fill();
      }
    }
    ctx.globalAlpha = 1;
    // manual knowledge gets a ring in the ink colour
    ctx.strokeStyle = this.colors.ink; ctx.lineWidth = 1; ctx.beginPath();
    for (const i of kb) { const r = this.nodeR(i) + 2, sx = g.x[i] * s + tx, sy = g.y[i] * s + ty; ctx.moveTo(sx + r, sy); ctx.arc(sx, sy, r, 0, Math.PI * 2); }
    ctx.globalAlpha = focus ? 0.4 : 0.8; ctx.stroke(); ctx.globalAlpha = 1;
    for (const [i, col, w] of [[this.hover, this.colors.ink, 1.5], [this.selected, this.colors.accent, 2.5]]) {
      if (i < 0 || !g.visible[i]) continue;
      const r = this.nodeR(i) + 3;
      ctx.strokeStyle = col; ctx.lineWidth = w; ctx.beginPath(); ctx.arc(g.x[i] * s + tx, g.y[i] * s + ty, r, 0, Math.PI * 2); ctx.stroke();
    }
    // Folded leaves: a count badge above the parent, once nodes are big enough.
    if (s >= 0.8 && badges.length) {
      ctx.font = `600 10px ${this.fonts.mono}`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillStyle = this.colors.muted; ctx.globalAlpha = focus ? 0.5 : 0.95;
      for (let k = 0; k < badges.length && k < 600; k++) {
        const i = badges[k], r = this.nodeR(i);
        ctx.fillText(`+${g.leafCount[i]}`, g.x[i] * s + tx, g.y[i] * s + ty - r - 7);
      }
      ctx.globalAlpha = 1; ctx.textAlign = 'left';
    }
  },
  // Overview first, details on demand (Shneiderman 1996): when single nodes
  // would be closer than a few pixels, draw one disc per community and the
  // summed edges between communities (Elmqvist and Fekete 2010).
  useOverview() {
    const g = graph;
    if (g.metaOnly) return true;
    if (this.detail === 'nodes' || g.C < 2) return false;
    if (this.detail === 'communities') return true;
    return g.n > 2500 && UNIT * this.scale < 4;
  },
  commRadius(c) { return Math.max(2.5, graph.cr[c] * this.scale); },
  commColor(c) {
    const g = graph;
    if (this.colorBy === 'community') { const p = palette(); const key = Number(g.cKey[c]); return key < 0 ? p[p.length - 1] : p[key % (p.length - 1)]; }
    return classColor(g.cClass[c]);
  },
  // Aggregated edges in three weight bands, heaviest first, with a budget.
  strokeOverviewEdges(ctx, x0, y0, x1, y1) {
    const g = graph, s = this.scale, tx = this.tx, ty = this.ty;
    const vis = g.cVisible, open = this.openComm;
    const sx = (c) => g.cx[c] * s + tx, sy = (c) => g.cy[c] * s + ty;
    const maxW = g.cEw.length ? g.cEw[0] : 1;
    ctx.strokeStyle = this.colors.edge; ctx.lineCap = 'round';
    // Widths up to 1 px take the fast hairline path; the heavy band is short.
    const bands = [[0.5, 0.16], [1, 0.3], [2.4, 0.42]];
    for (let band = 0; band < bands.length; band++) {
      ctx.lineWidth = bands[band][0]; ctx.globalAlpha = open >= 0 ? bands[band][1] * 0.5 : bands[band][1]; ctx.beginPath();
      for (let j = 0, drawn = 0; j < g.cEs.length && drawn < 6000; j++) {
        const a = g.cEs[j], b = g.cEt[j];
        if (vis && (!vis[a] || !vis[b])) continue;
        const rel = Math.log(1 + g.cEw[j]) / Math.log(1 + maxW);
        if ((rel > 0.66 ? 2 : rel > 0.33 ? 1 : 0) !== band) continue;
        const ax = sx(a), ay = sy(a), bx = sx(b), by = sy(b);
        if ((ax < x0 && bx < x0) || (ax > x1 && bx > x1) || (ay < y0 && by < y0) || (ay > y1 && by > y1)) continue;
        ctx.moveTo(ax, ay); ctx.lineTo(bx, by); drawn++;
      }
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  },
  drawOverview() {
    const ctx = this.ctx, g = graph, s = this.scale, tx = this.tx, ty = this.ty, W = this.w, H = this.h;
    const vis = g.cVisible, open = this.openComm;
    const sx = (c) => g.cx[c] * s + tx, sy = (c) => g.cy[c] * s + ty;
    // community discs, big ones first so small ones stay on top
    for (const c of g.cOrder) {
      if (vis && !vis[c]) continue;
      // The disc fills 70 % of its region, so the links between groups show.
      const x = sx(c), y = sy(c), r = c === open ? this.commRadius(c) : this.commRadius(c) * 0.7;
      if (x + r < 0 || x - r > W || y + r < 0 || y - r > H) continue;
      ctx.fillStyle = this.commColor(c);
      ctx.globalAlpha = c === open ? 0.12 : open >= 0 ? 0.3 : r > 12 ? 0.55 : 0.85;
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = 1;
    for (const [c, col, w] of [[this.hoverComm, this.colors.ink, 1.5], [open, this.colors.accent, 2]]) {
      if (c < 0) continue;
      ctx.strokeStyle = col; ctx.lineWidth = w;
      ctx.beginPath(); ctx.arc(sx(c), sy(c), (c === open ? this.commRadius(c) : this.commRadius(c) * 0.7) + 2, 0, Math.PI * 2); ctx.stroke();
    }
    // Drill-down: the open community shows its nodes inside its disc.
    if (open >= 0 && g.n) this.drawMembers([open], false);
    // A selected node keeps its neighbourhood at node detail.
    if (this.selected >= 0 && g.n && g.visible[this.selected] && g.ci[this.selected] !== open) {
      const i0 = this.selected, x0 = g.x[i0] * s + tx, y0 = g.y[i0] * s + ty;
      ctx.strokeStyle = this.colors.accent; ctx.globalAlpha = 0.85; ctx.lineWidth = 1.2; ctx.beginPath();
      for (const [, o] of g.neighbors(i0)) { if (!g.visible[o]) continue; ctx.moveTo(x0, y0); ctx.lineTo(g.x[o] * s + tx, g.y[o] * s + ty); }
      ctx.stroke(); ctx.globalAlpha = 1;
      ctx.fillStyle = this.colors.ink2; ctx.beginPath();
      for (const [, o] of g.neighbors(i0)) { if (!g.visible[o]) continue; const ox = g.x[o] * s + tx, oy = g.y[o] * s + ty; ctx.moveTo(ox + 2.5, oy); ctx.arc(ox, oy, 2.5, 0, Math.PI * 2); }
      ctx.fill();
      ctx.fillStyle = this.colors.accent; ctx.beginPath(); ctx.arc(x0, y0, 5, 0, Math.PI * 2); ctx.fill();
    }
    if (open >= 0 && g.n) this.drawLabels([open]);
    this.drawCommunityLabels();
  },
  // Labels by level of detail: the hovered and the open community always,
  // then the others by size while their disc is big enough and the text fits.
  drawCommunityLabels() {
    if (this.labels === 'off') return;
    const ctx = this.ctx, g = graph, s = this.scale, tx = this.tx, ty = this.ty, W = this.w, H = this.h;
    const placed = [];
    ctx.textBaseline = 'middle'; ctx.lineJoin = 'round'; ctx.textAlign = 'left';
    const put = (text, x, y, strong) => {
      ctx.font = `${strong ? 600 : 500} 12px ${this.fonts.body}`;
      const t = shortName(text, 30);
      const w = ctx.measureText(t).width;
      const box = [x - w / 2 - 3, y - 9, x + w / 2 + 3, y + 9];
      if (box[2] < 0 || box[0] > W || box[3] < 0 || box[1] > H) return;
      if (!strong) for (const p of placed) if (box[0] < p[2] && box[2] > p[0] && box[1] < p[3] && box[3] > p[1]) return;
      placed.push(box);
      ctx.strokeStyle = this.colors.paper; ctx.lineWidth = 4; ctx.globalAlpha = 0.92; ctx.strokeText(t, x - w / 2, y);
      ctx.globalAlpha = 1; ctx.fillStyle = strong ? this.colors.ink : this.colors.ink2; ctx.fillText(t, x - w / 2, y);
    };
    const above = (c) => g.cy[c] * s + ty - this.commRadius(c) - 10;
    if (this.selected >= 0 && g.n) put(g.labels[this.selected], g.x[this.selected] * s + tx, g.y[this.selected] * s + ty - 14, true);
    if (this.openComm >= 0) put(g.communityName(this.openComm), g.cx[this.openComm] * s + tx, above(this.openComm), true);
    if (this.hoverComm >= 0 && this.hoverComm !== this.openComm) { const c = this.hoverComm; put(`${g.communityName(c)} \u00b7 ${num(g.cSize[c])}`, g.cx[c] * s + tx, above(c), true); }
    const budget = this.labels === 'more' ? 160 : 60, minR = this.labels === 'more' ? 6 : 12;
    let shown = 0;
    for (const c of g.cOrder) {
      if (shown >= budget) break;
      if (c === this.openComm || (g.cVisible && !g.cVisible[c])) continue;
      if (this.commRadius(c) < minR) break;
      const before = placed.length;
      put(g.communityName(c), g.cx[c] * s + tx, g.cy[c] * s + ty, false);
      if (placed.length > before) shown++;
    }
  },
  pickCommunity(sx, sy) {
    const g = graph; let best = -1, bestD = Infinity;
    for (let c = 0; c < g.C; c++) {
      if (g.cVisible && !g.cVisible[c]) continue;
      const dx = g.cx[c] * this.scale + this.tx - sx, dy = g.cy[c] * this.scale + this.ty - sy;
      const d = Math.sqrt(dx * dx + dy * dy), r = Math.max(this.commRadius(c), this.reach);
      // The smallest disc under the pointer wins, so small groups stay pickable.
      if (d <= r && r < bestD) { best = c; bestD = r; }
    }
    return best;
  },
  openCommunity(c, opts = {}) {
    if (c < 0) return;
    this.openComm = c; this.hoverComm = -1;
    if (!opts.keepCamera) this.fitCommunity(c);
    this.dirty = true;
    renderCommunity(c);
  },
  closeCommunity() {
    if (this.openComm < 0) return false;
    this.openComm = -1; this.dirty = true;
    return true;
  },
  setNote() {
    const g = graph;
    let text = '';
    if (g.metaOnly) text = `${num(g.C)} communities. Loading the nodes.`;
    else if (g.running) text = 'Arranging the nodes';
    else if (this.overview && this.openComm < 0) text = `${num(g.C)} communities. Zoom in or pick one to see its nodes.`;
    if (text !== this.noteText) { this.noteText = text; const note = $('#map-note'); if (note) note.textContent = text; }
  },
  // Node labels by level of detail: the selection first, then the hubs of
  // the communities in view, with a degree floor that falls as you zoom in.
  drawLabels(comms) {
    if (this.labels === 'off') return;
    const ctx = this.ctx, g = graph, s = this.scale, tx = this.tx, ty = this.ty;
    const placed = [];
    ctx.font = `500 12px ${this.fonts.body}`; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round'; ctx.textAlign = 'left';
    const tryLabel = (i, strong) => {
      if (i < 0 || !g.visible[i] || !this.inView(i)) return;
      const text = shortName(g.labels[i], 36);
      const sx = g.x[i] * s + tx + this.nodeR(i) + 4, sy = g.y[i] * s + ty;
      const w = ctx.measureText(text).width;
      const box = [sx - 2, sy - 8, sx + w + 2, sy + 8];
      if (!strong) for (const p of placed) if (box[0] < p[2] && box[2] > p[0] && box[1] < p[3] && box[3] > p[1]) return;
      placed.push(box);
      ctx.strokeStyle = this.colors.paper; ctx.lineWidth = 3.5; ctx.globalAlpha = 0.9; ctx.strokeText(text, sx, sy);
      ctx.globalAlpha = 1; ctx.fillStyle = strong ? this.colors.ink : this.colors.ink2; ctx.fillText(text, sx, sy);
    };
    tryLabel(this.selected, true);
    tryLabel(this.hover, true);
    if (this.focusSet) { let c = 0; for (const i of this.focusSet) { if (c++ > 60) break; tryLabel(i, false); } }
    const budget = this.labels === 'more' ? 400 : 140;
    const minDeg = s > 2.5 ? 0 : s > 1 ? 1 : s > 0.4 ? 3 : 8;
    const cand = [];
    for (const c of comms) {
      for (let p = g.mStart[c]; p < g.mStart[c + 1]; p++) {
        const i = g.mList[p];
        if (g.deg[i] < minDeg) break;
        if (this.focusSet && !this.focusSet.has(i)) continue;
        if (g.visible[i] && this.inView(i)) cand.push(i);
      }
    }
    cand.sort((a, b) => g.deg[b] - g.deg[a]);
    let shown = 0;
    for (const i of cand) {
      if (shown >= budget) break;
      const before = placed.length; tryLabel(i, false); if (placed.length > before) shown++;
    }
  },
  bind() {
    const cv = this.canvas;
    // Pointer Events cover mouse, pen and touch. One pointer pans or drags a
    // node; two pointers pinch to zoom; a tap without movement selects.
    const pts = new Map();
    let down = null, pinch = null;
    const local = (ev) => { const r = cv.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; };
    const pinchState = () => {
      const [a, b] = Array.from(pts.values());
      return { d: Math.hypot(a[0] - b[0], a[1] - b[1]) || 1, cx: (a[0] + b[0]) / 2, cy: (a[1] + b[1]) / 2 };
    };
    const hit = (x, y) => {
      if (!graph.n) return { node: -1, comm: this.overview ? this.pickCommunity(x, y) : -1 };
      if (this.overview) {
        // The open community and a selected node's neighbours stay pickable.
        let i = this.openComm >= 0 ? this.pick(x, y) : -1;
        if (i >= 0 && graph.ci[i] !== this.openComm) i = -1;
        if (i < 0 && this.selected >= 0) i = this.pick(x, y, this.focusSet);
        return i >= 0 ? { node: i, comm: -1 } : { node: -1, comm: this.pickCommunity(x, y) };
      }
      return { node: this.pick(x, y), comm: -1 };
    };
    cv.addEventListener('pointerdown', (ev) => {
      if (ev.button > 0) return;
      // The pointer can already be gone (a fast tap after pointercancel).
      try { cv.setPointerCapture(ev.pointerId); } catch { /* no capture */ }
      this.reach = ev.pointerType === 'touch' ? 22 : ev.pointerType === 'pen' ? 14 : 10;
      const [x, y] = local(ev);
      pts.set(ev.pointerId, [x, y]);
      if (pts.size === 2) { down = null; const p = pinchState(); pinch = { ...p, scale: this.scale, tx: this.tx, ty: this.ty }; return; }
      if (pts.size > 2) return;
      const t = hit(x, y);
      // Touch never drags nodes: a finger on a dense map would pin by accident.
      down = { x, y, tx: this.tx, ty: this.ty, node: ev.pointerType === 'touch' ? -1 : t.node, moved: false };
    });
    cv.addEventListener('pointermove', (ev) => {
      const [x, y] = local(ev);
      if (pts.has(ev.pointerId)) pts.set(ev.pointerId, [x, y]);
      if (pinch && pts.size === 2) {
        const p = pinchState();
        const ns = Math.min(Math.max(pinch.scale * p.d / pinch.d, 0.005), 30), k = ns / pinch.scale;
        this.scale = ns;
        this.tx = p.cx - (pinch.cx - pinch.tx) * k; this.ty = p.cy - (pinch.cy - pinch.ty) * k;
        this.fitPending = false; this.followSet = null; this.dirty = true;
        return;
      }
      if (down) {
        const dx = x - down.x, dy = y - down.y;
        if (!down.moved && Math.hypot(dx, dy) > (ev.pointerType === 'touch' ? 8 : 3)) down.moved = true;
        if (!down.moved) return;
        this.fitPending = false; this.followSet = null;
        const [wx, wy] = this.toWorld(x, y);
        if (down.node >= 0 && !S.readOnly && !this.overview && graph.pin(down.node, wx, wy)) this.invalidateGrid();
        else { down.node = -1; this.tx = down.tx + dx; this.ty = down.ty + dy; }
        this.dirty = true;
        return;
      }
      if (ev.pointerType !== 'mouse') return;
      this.reach = 10;
      const t = hit(x, y);
      if (t.node !== this.hover || t.comm !== this.hoverComm) {
        this.hover = t.node; this.hoverComm = t.comm; this.dirty = true;
        cv.style.cursor = t.node >= 0 || t.comm >= 0 ? 'pointer' : '';
      }
    });
    const up = (ev) => {
      const was = pts.size;
      pts.delete(ev.pointerId);
      if (pinch) { if (pts.size < 2) pinch = null; if (pts.size === 1) { const [p] = Array.from(pts.values()); down = { x: p[0], y: p[1], tx: this.tx, ty: this.ty, node: -1, moved: true }; } return; }
      if (!down || was > 1) { down = null; return; }
      const d = down; down = null;
      if (d.moved) return;
      const [x, y] = local(ev);
      const t = hit(x, y);
      if (t.node >= 0) openNode(graph.ids[t.node], { keepCamera: true });
      else if (t.comm >= 0 && t.comm !== this.openComm) this.openCommunity(t.comm);
      else { if (this.closeCommunity()) renderInspectorEmpty(); closeNode(); }
    };
    cv.addEventListener('pointerup', up);
    cv.addEventListener('pointercancel', (ev) => { pts.delete(ev.pointerId); if (pts.size < 2) pinch = null; down = null; });
    cv.addEventListener('pointerleave', (ev) => { if (ev.pointerType === 'mouse' && (this.hover >= 0 || this.hoverComm >= 0)) { this.hover = -1; this.hoverComm = -1; this.dirty = true; } });
    cv.addEventListener('dblclick', (ev) => { const [x, y] = local(ev); const t = hit(x, y); if (t.node >= 0) this.centerOn(t.node, Math.max(this.scale * 2, 1.5)); else this.zoomAt(x, y, 2); });
    cv.addEventListener('wheel', (ev) => {
      ev.preventDefault();
      const [x, y] = local(ev);
      this.zoomAt(x, y, Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0015)));
    }, { passive: false });
    cv.addEventListener('keydown', (ev) => {
      const step = 60;
      if (ev.key === '+' || ev.key === '=') this.zoomAt(this.w / 2, this.h / 2, 1.25);
      else if (ev.key === '-') this.zoomAt(this.w / 2, this.h / 2, 0.8);
      else if (ev.key === 'ArrowLeft') this.tx += step; else if (ev.key === 'ArrowRight') this.tx -= step;
      else if (ev.key === 'ArrowUp') this.ty += step; else if (ev.key === 'ArrowDown') this.ty -= step;
      else if (ev.key === 'f') this.fit();
      else if (ev.key === 'Escape') { if (!this.closeCommunity()) closeNode(); else renderInspectorEmpty(); }
      else return;
      ev.preventDefault(); this.fitPending = false; this.followSet = null; this.dirty = true;
    });
  },
  zoomAt(sx, sy, f) {
    const ns = Math.min(Math.max(this.scale * f, 0.005), 30);
    const k = ns / this.scale;
    this.tx = sx - (sx - this.tx) * k; this.ty = sy - (sy - this.ty) * k; this.scale = ns;
    this.fitPending = false; this.followSet = null; this.dirty = true;
  },
};

// --------------------------------------------------------------- graph view
const ui = {
  hiddenClasses: new Set(),
  layoutState() {
    const b = $('#layout-toggle');
    if (b) { b.textContent = graph.running ? 'Pause layout' : 'Run layout'; b.setAttribute('aria-pressed', graph.running ? 'true' : 'false'); }
    map.noteText = null; map.setNote();
  },
};
let mapHost = null;
let currentNode = null;

function renderGraphView(root) {
  const left = h('aside', { class: 'pane left', id: 'map-settings', 'aria-label': 'Map settings' });
  const center = h('section', { class: 'map', 'aria-label': 'Graph map' });
  const right = h('aside', { class: 'pane right', id: 'inspector', 'aria-label': 'Inspector' });
  const scrim = h('div', { class: 'scrim', 'aria-hidden': 'true', onclick: () => setDrawer(false) });
  root.append(h('div', { class: 'view workbench', dataset: { drawer: 'closed', sheet: S.route.arg ? 'open' : 'closed' } }, left, center, right, scrim));
  renderLegend(left);
  if (!S.view && !graph.metaOnly) { center.append(skeletonState('Loading the graph')); return; }
  if (!graph.n && !graph.metaOnly) {
    center.append(h('div', { class: 'map-state' },
      h('h2', { text: 'The graph is empty' }),
      h('p', { text: 'Nodes from graphify extraction, documents and manual entries appear here.' }),
      S.readOnly ? null : h('button', { class: 'primary', onclick: () => renderAddNode() }, 'Add a node')));
  }
  mapHost = center;
  map.mount(center);
  center.append(h('div', { class: 'map-tools' },
    h('button', { class: 'drawer-toggle', 'aria-controls': 'map-settings', 'aria-expanded': 'false', onclick: () => setDrawer(true) }, 'Settings'),
    h('button', { onclick: () => { map.fit(); } }, 'Fit'),
    h('button', { id: 'layout-toggle', 'aria-pressed': 'false', onclick: () => (graph.running ? graph.stopLayout() : graph.reheat()) }, 'Run layout'),
    S.readOnly ? null : h('button', { class: 'sheet-only', onclick: () => renderAddNode() }, 'Add node')));
  center.append(h('p', { class: 'map-note', id: 'map-note', 'aria-live': 'polite' }));
  ui.layoutState();
  if (S.route.arg) openNode(S.route.arg, { fromRoute: true });
  else if (S.route.q.c !== undefined && graph.communityOf(S.route.q.c) >= 0) { map.openComm = -1; syncCommunity(); }
  else { map.openComm = -1; renderInspectorEmpty(); }
}

// On narrow screens the map settings slide in as a drawer and the inspector
// rises as a bottom sheet. Wide screens show both panes and ignore the state.
function setDrawer(open) {
  const wb = $('.workbench'); if (!wb) return;
  wb.dataset.drawer = open ? 'open' : 'closed';
  const t = $('.drawer-toggle'); if (t) t.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) { const b = $('#map-settings button'); if (b) b.focus(); }
  else if (t && document.activeElement && $('#map-settings') && $('#map-settings').contains(document.activeElement)) t.focus();
}
function setSheet(open) {
  const wb = $('.workbench'); if (wb) wb.dataset.sheet = open ? 'open' : 'closed';
}

function renderLegend(left) {
  const mode = S.route.q.color || 'class';
  map.colorBy = mode;
  map.labels = S.route.q.labels || 'auto';
  map.detail = S.route.q.detail || 'auto';
  const leaves = S.route.q.leaves === 'shown';
  if (graph.showLeaves !== leaves) { graph.showLeaves = leaves; graph.applyFilter(); }
  map.dirty = true;
  const seg = (name, value, label, current) => h('button', { 'aria-pressed': current === value ? 'true' : 'false', onclick: () => patchQuery({ [name]: value === 'class' || value === 'auto' ? '' : value }) }, label);
  left.append(h('div', { class: 'drawer-head' }, h('h2', { text: 'Map settings' }), h('button', { class: 'quiet', onclick: () => setDrawer(false) }, 'Close')));
  left.append(h('section', { class: 'stack' },
    h('h2', { text: 'Show' }),
    h('div', { class: 'segmented', role: 'group', 'aria-label': 'Show' }, seg('detail', 'auto', 'Auto', map.detail), seg('detail', 'communities', 'Communities', map.detail), seg('detail', 'nodes', 'Nodes', map.detail)),
    h('p', { class: 'hint', text: 'Auto draws communities when the map is zoomed out and single nodes when you zoom in.' }),
    h('h3', { text: 'Colour by' }),
    h('div', { class: 'segmented', role: 'group', 'aria-label': 'Colour by' }, seg('color', 'class', 'Class', mode), seg('color', 'community', 'Community', mode)),
    h('h3', { text: 'Labels' }),
    h('div', { class: 'segmented', role: 'group', 'aria-label': 'Labels' }, seg('labels', 'auto', 'Hubs', map.labels), seg('labels', 'more', 'More', map.labels), seg('labels', 'off', 'Off', map.labels)),
    h('h3', { text: 'Leaves' }),
    h('div', { class: 'segmented', role: 'group', 'aria-label': 'Leaves' },
      h('button', { 'aria-pressed': leaves ? 'false' : 'true', onclick: () => patchQuery({ leaves: '' }) }, 'Folded'),
      h('button', { 'aria-pressed': leaves ? 'true' : 'false', onclick: () => patchQuery({ leaves: 'shown' }) }, 'Shown')),
    graph.folded ? h('p', { class: 'hint', text: `${num(graph.folded)} nodes with one link sit folded under their neighbour. Select the neighbour to show them.` }) : null));
  if (!S.view) { left.append(skeletonState('Loading')); return; }
  const list = h('ul', { class: 'legend' });
  if (mode === 'class') {
    for (const name of graph.classNames) {
      const count = graph.classCounts.get(name) || 0;
      const cb = h('input', { type: 'checkbox', checked: !ui.hiddenClasses.has(name), onchange: (ev) => { if (ev.target.checked) ui.hiddenClasses.delete(name); else ui.hiddenClasses.add(name); graph.applyFilter(); } });
      const known = S.onto && S.onto.classes.some((c) => c.name === name);
      list.append(h('li', null, h('label', { title: known ? name : `${name} is not in the ontology` }, cb, h('span', { class: 'swatch', style: `--swatch:${classColor(name)}` }), h('span', { class: 'name' }, QUIET_CLASSES[name] ? QUIET_CLASSES[name].label : name, known ? '' : h('span', { class: 'missing', text: ' (undefined)' })), h('span', { class: 'figure', text: num(count) }))));
    }
    left.append(h('section', null, h('h2', { text: 'Classes' }), h('p', { class: 'hint', text: 'Clear a box to hide that class.' }), list));
  } else {
    for (const c of graph.cOrder.slice(0, 24)) {
      list.append(h('li', null, h('button', { type: 'button', class: 'legend-row', onclick: () => { setDrawer(false); map.openCommunity(c); } },
        h('span'), h('span', { class: 'swatch', style: `--swatch:${map.commColor(c)}` }), h('span', { class: 'name', text: graph.communityName(c) }), h('span', { class: 'figure', text: num(graph.cSize[c]) }))));
    }
    left.append(h('section', null, h('h2', { text: 'Largest communities' }), h('p', { class: 'hint', text: `${num(graph.C)} communities. Pick one to zoom to it.` }), list));
  }
  const rels = new Map();
  for (let e = 0; e < graph.m; e++) rels.set(graph.er[e], (rels.get(graph.er[e]) || 0) + 1);
  const rl = h('ul', { class: 'legend' });
  for (const [r, c] of Array.from(rels.entries()).sort((a, b) => b[1] - a[1]).slice(0, 16)) {
    const known = S.onto && S.onto.relations.some((x) => x.name === graph.relations[r]);
    rl.append(h('li', null, h('div', { class: 'legend-row' }, h('span'), h('span'), h('span', { class: 'name' }, graph.relations[r], known ? '' : h('span', { class: 'missing', text: ' (undefined)' })), h('span', { class: 'figure', text: num(c) }))));
  }
  left.append(h('section', null, h('h2', { text: 'Relations' }), rl));
}

function skeletonState(label) {
  return h('div', { class: 'skeleton', 'aria-busy': 'true', 'aria-label': label }, h('span'), h('span'), h('span'), h('span'), h('span'));
}
function failedState(err, retry) {
  return h('div', { class: 'failed' }, h('p', { text: err && err.message ? err.message : String(err) }), retry ? h('button', { onclick: retry }, 'Try again') : null);
}

// Community inspector (drill-down): what a group holds and what it links to.
function renderCommunity(c) {
  const el = $('#inspector'); if (!el) return;
  const g = graph;
  currentNode = null;
  if (map.selected >= 0) map.select(-1);
  setSheet(true);
  const key = String(g.cKey[c]);
  if (S.route.tab === 'graph' && (S.route.q.c !== key || S.route.arg)) setRoute('graph', '', { ...S.route.q, c: key }, true);
  const close = () => { map.closeCommunity(); patchQuery({ c: '' }); map.fit(); renderInspectorEmpty(); };
  const title = h('div', { class: 'inspector-title' }, h('h2', null, breakable(g.communityName(c))),
    h('button', { class: 'quiet', 'aria-label': 'Back to the overview', onclick: close }, 'Overview'));
  if (!g.n) {
    el.replaceChildren(h('section', { class: 'stack' }, title, skeletonState('Loading the nodes')));
    return;
  }
  const classes = new Map(), top = [];
  let kb = 0;
  for (let p = g.mStart[c]; p < g.mStart[c + 1]; p++) {
    const i = g.mList[p];
    if (g.kb[i]) kb++;
    const cl = g.classOf[g.cls[i]]; classes.set(cl, (classes.get(cl) || 0) + 1);
    if (top.length < 10) top.push(i);
  }
  const kv = h('dl', { class: 'kv' },
    h('dt', { text: 'Nodes' }), h('dd', null, h('span', { class: 'figure', text: num(g.cSize[c]) })),
    kb ? [h('dt', { text: 'Manual' }), h('dd', null, h('span', { class: 'figure', text: num(kb) }))] : null,
    h('dt', { text: 'Classes' }), h('dd', null, h('ul', { class: 'conn-list' }, Array.from(classes).sort((a, b) => b[1] - a[1]).map(([cl, n]) =>
      h('li', null, h('span', { class: 'swatch', style: `--swatch:${classColor(cl)}` }), h('span', { text: cl }), h('span', { class: 'figure muted', text: num(n) }))))));
  const hubs = h('ul', { class: 'conn-list' }, top.map((i) => h('li', null, h('span', { class: 'swatch', style: `--swatch:${map.nodeColor(i)}` }),
    h('button', { class: 'link', onclick: () => setRoute('graph', g.ids[i], S.route.q) }, breakable(g.labels[i])), h('span', { class: 'figure muted', text: num(g.deg[i]) }))));
  const near = [];
  for (let j = 0; j < g.cEs.length && near.length < 10; j++) {
    if (g.cEs[j] === c) near.push([g.cEt[j], g.cEw[j]]); else if (g.cEt[j] === c) near.push([g.cEs[j], g.cEw[j]]);
  }
  const links = near.length ? h('ul', { class: 'conn-list' }, near.map(([o, w]) => h('li', null, h('span', { class: 'swatch', style: `--swatch:${map.commColor(o)}` }),
    h('button', { class: 'link', onclick: () => map.openCommunity(o) }, breakable(g.communityName(o))), h('span', { class: 'figure muted', text: num(w) }))))
    : h('p', { class: 'muted', text: 'No links to other communities.' });
  el.replaceChildren(h('section', { class: 'stack' }, title, kv),
    h('section', { class: 'stack' }, h('h3', { text: 'Hubs' }), hubs),
    h('section', { class: 'stack' }, h('h3', { text: 'Linked communities' }), links));
}

// The ?c= query opens one community; keep the map in step with it.
function syncCommunity() {
  const key = S.route.q.c;
  const c = key === undefined || !graph.C ? -1 : graph.communityOf(key);
  if (c === map.openComm) return;
  if (c >= 0) map.openCommunity(c); else if (map.closeCommunity()) { if (!S.route.arg) renderInspectorEmpty(); }
}

function renderInspectorEmpty() {
  const el = $('#inspector'); if (!el) return;
  setSheet(false);
  el.replaceChildren(h('section', { class: 'stack' },
    h('h2', { text: 'Nothing selected' }),
    h('p', { class: 'muted', text: 'Pick a node on the map or search for one. The inspector shows its class, attributes and connections.' }),
    S.readOnly ? null : h('button', { class: 'primary', onclick: () => renderAddNode() }, 'Add a node')));
}

function ontologyClasses() { return S.onto ? S.onto.classes.map((c) => c.name) : graph.classNames; }
function ontologyRelations() { return S.onto ? S.onto.relations.map((r) => r.name) : graph.relations; }
function datalist(id, values) { return h('datalist', { id }, values.map((v) => h('option', { value: v }))); }

function renderAddNode(prefill = {}) {
  if (S.route.tab !== 'graph') { setRoute('graph'); setTimeout(() => renderAddNode(prefill), 0); return; }
  const el = $('#inspector'); if (!el) return;
  setSheet(true);
  const label = h('input', { id: 'new-label', required: true, value: prefill.label || '', placeholder: 'For example: Payment service' });
  const cls = h('input', { id: 'new-class', list: 'class-options', value: prefill.cls || 'concept' });
  const err = h('p', { class: 'error-text', role: 'alert' });
  const form = h('form', { class: 'stack', onsubmit: async (ev) => {
    ev.preventDefault();
    if (!label.value.trim()) { label.setAttribute('aria-invalid', 'true'); err.textContent = 'Give the node a label.'; return; }
    try {
      const node = await api('POST', '/api/nodes', { label: label.value.trim(), class: cls.value.trim() || undefined });
      await refreshAll();
      undoable(`Added ${node.label}.`, node);
      setRoute('graph', node.id, S.route.q);
    } catch (e) { err.textContent = e.message; }
  } },
    h('h2', { text: 'Add a node' }),
    h('label', { class: 'field', for: 'new-label' }, h('span', { text: 'Label' }), label),
    h('label', { class: 'field', for: 'new-class' }, h('span', { text: 'Class' }), cls, datalist('class-options', ontologyClasses())),
    h('p', { class: 'hint', text: 'A class outside the ontology is allowed. It shows up under Findings until you define it.' }),
    err,
    h('div', { class: 'row' }, h('button', { class: 'primary', type: 'submit' }, 'Add node'), h('button', { type: 'button', class: 'quiet', onclick: () => (currentNode ? openNode(currentNode) : renderInspectorEmpty()) }, 'Cancel')));
  el.replaceChildren(form);
  label.focus();
}

// Keyboard focus: a search pick moves focus to the inspector title, and closing
// the node gives it back to the search field. A refresh that rebuilds the page
// gives it back to the same field of the node (field = its data-field).
const focusNav = { title: false, field: '', back: null };
function closeNode() {
  currentNode = null; map.select(-1);
  if (S.route.tab === 'graph' && S.route.arg) setRoute('graph', '', S.route.q, true);
  renderInspectorEmpty();
  const back = focusNav.back; focusNav.back = null;
  if (back && back.isConnected) { back.dataset.quiet = '1'; back.focus(); }
}

// A click on the map keeps the camera: the user already chose that view.
// Search, links and URLs frame the node with its neighbours.
let keepCameraFor = null;
async function openNode(id, opts = {}) {
  if (S.route.tab !== 'graph') { setRoute('graph', id, S.route.q); return; }
  if (!opts.fromRoute && S.route.arg !== id) { keepCameraFor = opts.keepCamera ? id : null; setRoute('graph', id, S.route.q, true); return; }
  const keep = opts.keepCamera || keepCameraFor === id; keepCameraFor = null;
  currentNode = id;
  const i = graph.index.has(id) ? graph.index.get(id) : -1;
  map.select(i, { center: opts.fromRoute && i >= 0 && !keep, zoom: 1.2 });
  const el = $('#inspector'); if (!el) return;
  // Keep focus in the inspector when a link inside it opens the next node.
  // Clear the focus request only when a live inspector uses it. A second rebuild
  // during the load replaces this inspector, and its own openNode needs the request.
  const focusTitle = focusNav.title || el.contains(document.activeElement);
  const field = focusNav.field;
  setSheet(true);
  el.replaceChildren(skeletonState('Loading the node'));
  let detail;
  try { detail = await nodeDetail(id); } catch (e) { if (el.isConnected) { focusNav.title = false; focusNav.field = ''; el.replaceChildren(failedState(e, () => openNode(id, opts))); } return; }
  if (currentNode !== id || !el.isConnected) return;
  focusNav.title = false; focusNav.field = '';
  renderInspector(el, detail);
  if (focusTitle) { const t = (field && $(`[data-field="${CSS.escape(field)}"]`, el)) || $('.inspector-title .editable', el) || $('.inspector-title h2', el); if (t) { if (!t.matches('button')) t.tabIndex = -1; t.focus({ preventScroll: true }); } }
}

async function nodeDetail(id) {
  if (!STATIC) return api('GET', `/api/nodes/${enc(id)}`);
  const i = graph.index.get(id);
  if (i === undefined) throw new Error(`Node ${id} does not exist.`);
  const extra = (STATIC.details && STATIC.details[i]) || {};
  const out = [], inc = [];
  for (const [e, o, outgoing] of graph.neighbors(i)) {
    const row = { id: '', relation: graph.relations[graph.er[e]], other: graph.ids[o], other_label: graph.labels[o], other_class: graph.classOf[graph.cls[o]] };
    (outgoing ? out : inc).push(row);
  }
  const cls = graph.classOf[graph.cls[i]];
  const spec = S.onto && S.onto.classes.find((c) => c.name === cls);
  return { node: { id, label: graph.labels[i], ...extra }, class: cls, kb: !!graph.kb[i], out, in: inc, out_total: out.length, in_total: inc.length, properties: spec ? spec.properties : [] };
}

const RESERVED = new Set(['id', 'label', 'class', 'file_type', 'source_file', 'source_location', 'community', 'community_name', 'norm_label', '_origin', '_src', '_tgt', 'doc_id', 'created_at', 'updated_at', 'actor', 'tags']);

function valueText(v) { return v === undefined ? '' : typeof v === 'string' ? v : JSON.stringify(v); }
function parseValue(text) {
  const t = text.trim();
  if (t === '') return null;
  if (/^(true|false|null|-?\d+(\.\d+)?|\[.*\]|\{.*\})$/s.test(t)) { try { return JSON.parse(t); } catch { /* keep text */ } }
  return text;
}

function editableValue(value, onSave, opts = {}) {
  const label = (v) => (v === '' || v === undefined ? (opts.placeholder || 'Empty') : v);
  const text = label(value);
  // opts.breakable: a code name breaks after / _ - . first, not inside a word.
  const show = (v) => (opts.breakable ? breakable(v) : v);
  // The name keeps the visible value (WCAG 2.5.3): a screen reader hears what it edits.
  const shown = h('button', { class: 'editable' + (opts.mono ? ' mono' : ''), type: 'button', title: 'Edit', 'aria-label': `Edit ${opts.label || 'value'}: ${text}`, 'data-field': opts.label || null, 'data-write': opts.alwaysShow ? null : true }, show(text));
  if (S.readOnly) return h('span', { class: opts.mono ? 'mono' : '' }, value ? show(value) : '');
  const setValue = (v) => {
    value = v;
    shown.replaceChildren(show(label(v)));
    shown.setAttribute('aria-label', `Edit ${opts.label || 'value'}: ${label(v)}`);
  };
  shown.addEventListener('click', () => {
    const input = h('input', { value: value || '', 'aria-label': opts.label || 'Value' });
    if (opts.list) input.setAttribute('list', opts.list);
    // Enter, Escape and the blur that follows them must act once: one save, one history entry.
    let done = false;
    // The field closes at once with the new value: the refresh after a save can take seconds.
    // A failed save shows the old value again. Only Enter moves the focus back to the value.
    const save = async (byKey) => {
      if (done) return;
      done = true;
      const next = input.value, before = value;
      if (next !== (before || '')) setValue(next);
      input.replaceWith(shown);
      if (byKey) shown.focus();
      if (next === (before || '')) return;
      try { await onSave(next); } catch (e) { fail(e); setValue(before); }
    };
    input.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); save(true); } else if (ev.key === 'Escape') { done = true; input.replaceWith(shown); shown.focus(); } });
    input.addEventListener('blur', () => save(false));
    shown.replaceWith(input); input.focus(); input.select();
  });
  return shown;
}

function renderInspector(el, d) {
  const node = d.node, id = node.id;
  const patchNode = async (attrs, message) => {
    const r = await api('PATCH', `/api/nodes/${enc(id)}`, { attrs });
    await refreshAll();
    undoable(message || `Updated ${node.label}.`, r);
  };
  const title = h('div', { class: 'inspector-title' },
    h('h2', null, editableValue(node.label, (v) => patchNode({ label: v }, `Renamed to ${v}.`), { label: 'Label', alwaysShow: true, breakable: true })),
    h('button', { class: 'quiet', 'aria-label': 'Close the inspector', onclick: closeNode }, 'Close'));
  const kv = h('dl', { class: 'kv' },
    h('dt', { text: 'Class' }), h('dd', null, editableValue(d.class, (v) => patchNode({ class: v.trim() || null }, `Set class ${v}.`), { list: 'class-options', label: 'Class', alwaysShow: true }), datalist('class-options', ontologyClasses())),
    h('dt', { text: 'Id' }), h('dd', null, h('span', { class: 'id', text: id })),
    node.source_file ? [h('dt', { text: 'Source' }), h('dd', null, h('span', { class: 'mono', text: node.source_file + (node.source_location ? ` ${node.source_location}` : '') }))] : null,
    node.community_name ? [h('dt', { text: 'Community' }), h('dd', { text: node.community_name })] : null,
    d.kb ? [h('dt', { text: 'Added by' }), h('dd', null, h('span', { class: 'mono', text: `${node.actor || 'api'} ${when(node.created_at)}` }))] : null);
  const props = h('dl', { class: 'kv' });
  const specNames = new Set();
  const unsetProps = [];
  for (const p of d.properties || []) {
    specNames.add(p.name);
    const has = node[p.name] !== undefined && node[p.name] !== null;
    // Optional properties without a value stay out of the list; the attribute form suggests them.
    if (!has && !p.required) { unsetProps.push(p.name); continue; }
    props.append(h('dt', null, p.name, p.required ? h('span', { class: 'required-mark', title: 'Required', text: ' *' }) : null),
      h('dd', { class: !has && p.required ? 'missing' : '' }, editableValue(valueText(node[p.name]), (v) => patchNode({ [p.name]: parseValue(v) }), { placeholder: p.required ? 'Missing' : 'Empty', label: p.name })));
  }
  for (const [k, v] of Object.entries(node)) {
    // Keys with a leading underscore are graphify bookkeeping, not attributes.
    if (RESERVED.has(k) || specNames.has(k) || k.startsWith('_')) continue;
    props.append(h('dt', { text: k }), h('dd', null, editableValue(valueText(v), (nv) => patchNode({ [k]: parseValue(nv) }), { label: k })));
  }
  const key = h('input', { placeholder: 'Name', 'aria-label': 'Attribute name', list: 'attr-options' });
  const val = h('input', { placeholder: 'Value', 'aria-label': 'Attribute value' });
  const addAttr = h('form', { class: 'row', 'data-write': true, onsubmit: async (ev) => { ev.preventDefault(); if (!key.value.trim()) return; try { await patchNode({ [key.value.trim()]: parseValue(val.value) }, `Set ${key.value.trim()}.`); } catch (e) { fail(e); } } },
    key, val, h('button', { type: 'submit' }, 'Set'), datalist('attr-options', unsetProps));
  const conns = h('section', null, h('h3', null, 'Connections ', h('span', { class: 'figure muted', text: num(d.out_total + d.in_total) })));
  for (const [label, rows, total] of [['Outgoing', d.out, d.out_total], ['Incoming', d.in, d.in_total]]) {
    if (!rows.length) continue;
    const groups = new Map();
    for (const r of rows) { if (!groups.has(r.relation)) groups.set(r.relation, []); groups.get(r.relation).push(r); }
    conns.append(h('h4', { class: 'muted', text: `${label}${total > rows.length ? ` (first ${num(rows.length)} of ${num(total)})` : ''}` }));
    for (const [rel, list] of groups) {
      const ul = h('ul', { class: 'conn-list' });
      // Same label twice: the source file (else the community) tells them apart.
      const seen = new Map();
      for (const r of list) seen.set(r.other_label, (seen.get(r.other_label) || 0) + 1);
      const where = (r) => {
        if (!(seen.get(r.other_label) > 1)) return '';
        if (r.other_source_file) return shortName(r.other_source_file, 40);
        const i = graph.index.get(r.other);
        return i !== undefined ? graph.communityName(graph.ci[i]) : '';
      };
      for (const r of list.slice(0, 60)) {
        const hint = where(r);
        ul.append(h('li', null, h('span', { class: 'swatch', style: `--swatch:${classColor(r.other_class || '')}` }),
          h('button', { class: 'link', title: r.other, onclick: () => setRoute('graph', r.other, S.route.q) }, breakable(r.other_label || r.other), hint ? h('span', { class: 'hint', text: ` · ${hint}` }) : null),
          r.id && !S.readOnly ? h('button', { class: 'quiet', 'aria-label': `Remove the ${rel} connection to ${r.other_label}`, onclick: async () => {
            try { const done = await api('DELETE', `/api/edges/${enc(r.id)}`); await refreshAll(); undoable(`Removed ${rel} to ${r.other_label}.`, done); openNode(id, { fromRoute: true, keepCamera: true }); } catch (e) { fail(e); }
          } }, 'Remove') : h('span')));
      }
      conns.append(h('div', { class: 'conn-group' }, h('h4', null, rel, ' ', h('span', { class: 'figure', text: num(list.length) })), ul));
    }
  }
  if (!d.out.length && !d.in.length) conns.append(h('p', { class: 'muted', text: 'No connections yet.' }));
  const rel = h('input', { list: 'relation-options', placeholder: 'Relation', 'aria-label': 'Relation', required: true });
  const target = h('input', { list: 'target-options', placeholder: 'Target node', 'aria-label': 'Target node', required: true });
  const targets = h('datalist', { id: 'target-options' });
  const dir = h('select', { 'aria-label': 'Direction' }, h('option', { value: 'out', text: 'From this node' }), h('option', { value: 'in', text: 'To this node' }));
  target.addEventListener('input', debounce(async () => {
    if (target.value.length < 2) return;
    try {
      const res = await api('GET', `/api/search?limit=12&q=${enc(target.value)}`);
      targets.replaceChildren(...res.results.filter((r) => r.kind === 'node').map((r) => h('option', { value: r.id, label: `${r.label} (${r.class})` })));
    } catch { /* hints only */ }
  }, 200));
  const linkForm = h('form', { class: 'stack', 'data-write': true, onsubmit: async (ev) => {
    ev.preventDefault();
    let other = target.value.trim();
    if (!graph.index.has(other)) {
      const hit = graph.labels.findIndex((l) => l.toLowerCase() === other.toLowerCase());
      if (hit >= 0) other = graph.ids[hit];
    }
    const [s, t] = dir.value === 'out' ? [id, other] : [other, id];
    try { const done = await api('POST', '/api/edges', { source: s, target: t, relation: rel.value.trim() }); await refreshAll(); undoable(`Linked ${rel.value.trim()}.`, done); openNode(id, { fromRoute: true, keepCamera: true }); } catch (e) { fail(e); }
  } }, h('h3', { text: 'Add a connection' }), h('div', { class: 'row' }, rel, dir), target, targets, datalist('relation-options', ontologyRelations()), h('button', { type: 'submit' }, 'Link'));
  const docs = (d.in || []).filter((r) => r.relation === 'mentions' && String(r.other).startsWith('doc:'));
  const docSection = h('section', null, h('h3', { text: 'Documents' }),
    docs.length ? h('ul', { class: 'conn-list' }, docs.map((r) => h('li', null, h('span'), h('button', { class: 'link', onclick: () => setRoute('documents', r.other.slice(4)) }, r.other_label), h('span')))) : h('p', { class: 'muted', text: 'No document mentions this node.' }),
    node.doc_id ? docOpenButton(node.doc_id) : null,
    S.readOnly ? null : h('button', { onclick: () => newDocument({ title: `Notes on ${node.label}`, nodes: [id] }) }, 'Write a document about this node'));
  const danger = h('section', { 'data-write': true }, h('button', { class: 'danger', onclick: async () => {
    try { const r = await api('DELETE', `/api/nodes/${enc(id)}`); closeNode(); await refreshAll(); undoable(`Deleted ${node.label}${r.edges_deleted ? ` and ${plural(r.edges_deleted, 'connection')}` : ''}.`, r); } catch (e) { fail(e); }
  } }, node.doc_id ? 'Delete node and document' : 'Delete node'));
  el.replaceChildren(h('section', { class: 'stack' }, title, kv), h('section', { class: 'stack' }, h('h3', { text: 'Attributes' }), props.children.length ? props : h('p', { class: 'muted', text: 'No attributes beyond the basics.' }), addAttr), conns, linkForm, docSection, danger);
}

// ------------------------------------------------------------ ontology view
function classUsage() { return graph.classCounts || new Map(); }
function relationUsage() {
  const out = new Map();
  for (let e = 0; e < graph.m; e++) { const r = graph.relations[graph.er[e]]; out.set(r, (out.get(r) || 0) + 1); }
  return out;
}

async function renderOntologyView(root) {
  const page = h('div', { class: 'page' }); const inner = h('div', { class: 'page-inner' }); page.append(inner); root.append(page);
  if (!S.onto) { inner.append(skeletonState('Loading the ontology')); try { await loadOnto(); } catch (e) { inner.replaceChildren(failedState(e, rerender)); return; } inner.replaceChildren(); }
  const o = S.onto;
  const cu = classUsage(), ru = relationUsage();
  const sel = S.route.q;
  const strict = h('input', { type: 'checkbox', id: 'strict', checked: !!o.strict, disabled: S.readOnly, onchange: async (ev) => {
    try { const data = { ...o, strict: ev.target.checked }; delete data.inferred; delete data.problems; S.onto = await api('PUT', '/api/ontology', data); S.report = null; undoable(ev.target.checked ? 'Strict mode is on.' : 'Strict mode is off.', S.onto); rerender(); } catch (e) { fail(e); }
  } });
  inner.append(h('div', { class: 'page-head' },
    h('div', null, h('h1', { text: 'Ontology' }), h('p', { text: o.inferred ? 'Inferred from the extracted graph. The first change saves it to ontology.json.' : 'Saved in ontology.json next to graph.json.' })),
    h('div', { class: 'row' },
      h('label', { class: 'row', for: 'strict' }, strict, 'Unknown classes and relations are errors'),
      h('button', { 'data-write': true, onclick: async () => { try { S.onto = await api('POST', '/api/ontology/infer', { save: true }); S.report = null; undoable('Inferred the ontology from the graph.', S.onto); rerender(); } catch (e) { fail(e); } } }, 'Infer from graph'),
      h('button', { 'data-write': true, onclick: () => patchQuery({ class: '', relation: '', new: 'class' }) }, 'Add class'),
      h('button', { 'data-write': true, class: 'primary', onclick: () => patchQuery({ class: '', relation: '', new: 'relation' }) }, 'Add relation'))));
  if (o.problems && o.problems.length) inner.append(h('div', { class: 'table-wrap' }, h('div', { class: 'failed' }, o.problems.map((p) => h('p', { text: p.message })))));
  const host = h('div', { class: 'onto-map', 'aria-label': 'Ontology map: classes and the relations between them' });
  inner.append(host);
  drawOntologyMap(host, o, cu, ru);
  const undefinedClasses = Array.from(cu.keys()).filter((c) => !o.classes.some((x) => x.name === c));
  const undefinedRelations = Array.from(ru.keys()).filter((r) => !o.relations.some((x) => x.name === r));
  if ((undefinedClasses.length || undefinedRelations.length) && !S.readOnly) {
    inner.append(h('section', { class: 'table-wrap' }, h('div', { class: 'empty' },
      h('h2', { text: 'Used in the graph, missing from the ontology' }),
      h('div', { class: 'row' }, undefinedClasses.map((c) => h('button', { class: 'chip', onclick: () => saveClass({ name: c, properties: [] }, `Defined class ${c}.`) }, `Define class ${c}`)),
        undefinedRelations.map((r) => h('button', { class: 'chip', onclick: () => saveRelation({ name: r, domain: [], range: [] }, `Defined relation ${r}.`) }, `Define relation ${r}`))))));
  }
  inner.append(h('div', { class: 'split' }, classTable(o, cu, sel), relationTable(o, ru, sel)));
}

async function saveClass(spec, message) {
  try { S.onto = await api('PUT', `/api/ontology/classes/${enc(spec.name)}`, spec); S.report = null; undoable(message || `Saved class ${spec.name}.`, S.onto); patchQuery({ class: spec.name, new: '' }); rerender(); } catch (e) { fail(e); }
}
async function saveRelation(spec, message) {
  try { S.onto = await api('PUT', `/api/ontology/relations/${enc(spec.name)}`, spec); S.report = null; undoable(message || `Saved relation ${spec.name}.`, S.onto); patchQuery({ relation: spec.name, new: '' }); rerender(); } catch (e) { fail(e); }
}

// The count of a class: the backend count when it has one, else the nodes of
// the loaded graph. A parent's total includes its subclasses.
function classCount(c, cu) { return typeof c.count === 'number' ? c.count : typeof c.support === 'number' ? c.support : (cu.get(c.name) || 0); }
// Readable names for the validation finding codes (ontology version 2).
const FINDING_LABELS = {
  unknown_class: 'Unknown class', unknown_relation: 'Unknown relation', missing_property: 'Missing property',
  wrong_type: 'Wrong value type', invalid_value: 'Invalid value', cardinality: 'Too many outgoing edges',
  in_cardinality: 'Too many incoming edges', domain: 'Wrong source class', range: 'Wrong target class',
  unlikely_domain: 'Unusual source class', unlikely_range: 'Unusual target class', missing_edge: 'Missing edge',
  cycle: 'Cycle', duplicate_entity: 'Possible duplicate', invalid_confidence: 'Invalid confidence',
  confidence_mismatch: 'Confidence does not match', nonconforming_extraction: 'Extraction breaks the schema',
  dangling_edge: 'Edge to a missing node',
};
function findingLabel(code) { return FINDING_LABELS[code] || String(code || '').replace(/_/g, ' '); }
// Why a rule exists: declared by a person, or inferred with its support and
// confidence (the share of cases that follow it).
function ruleText(spec) {
  if (!spec || spec.origin === 'declared') return spec && spec.origin ? 'Declared' : '';
  const parts = ['Inferred'];
  if (typeof spec.support === 'number') parts.push(`${num(spec.support)} seen`);
  if (typeof spec.confidence === 'number') parts.push(`${(spec.confidence * 100).toFixed(spec.confidence > 0.99 && spec.confidence < 1 ? 1 : 0)}% hold`);
  return parts.join(' · ');
}
function edgeShapeText(e) {
  const dir = e.direction === 'in' ? 'incoming' : 'outgoing';
  const held = typeof e.confidence === 'number' ? ` · ${(e.confidence * 100).toFixed(1)}% hold` : '';
  return `Needs ${e.min_count > 1 ? 'at least ' + e.min_count : 'one'} ${dir} ${e.relation} edge${e.min_count > 1 ? 's' : ''}${held}`;
}
function profileText(profile) {
  return Object.entries(profile || {}).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([c, n]) => `${c} ${num(n)}`).join(' · ');
}
function matchText(match) {
  if (!match || typeof match !== 'object') return '';
  return Object.entries(match).map(([k, v]) => `${k} ${Array.isArray(v) ? 'in ' + v.join(', ') : typeof v === 'object' && v !== null ? JSON.stringify(v) : '= ' + v}`).join('; ');
}
function classTree(o) {
  const byName = new Map(o.classes.map((c) => [c.name, c]));
  const kids = new Map();
  const roots = [];
  for (const c of o.classes) {
    // A parent outside the ontology or a cycle makes the class a root.
    let p = c.parent && byName.has(c.parent) && c.parent !== c.name ? c.parent : null;
    for (let seen = new Set([c.name]), q = p; q; q = byName.get(q) && byName.get(q).parent) { if (seen.has(q)) { p = null; break; } seen.add(q); }
    if (p) { if (!kids.has(p)) kids.set(p, []); kids.get(p).push(c); } else roots.push(c);
  }
  return { roots, kids };
}

function classTable(o, cu, sel) {
  const body = h('tbody');
  const { roots, kids } = classTree(o);
  const total = new Map();
  const sum = (c) => { if (total.has(c.name)) return total.get(c.name); total.set(c.name, 0); const t = classCount(c, cu) + (kids.get(c.name) || []).reduce((s, k) => s + sum(k), 0); total.set(c.name, t); return t; };
  o.classes.forEach(sum);
  const bySize = (a, b) => total.get(b.name) - total.get(a.name) || a.name.localeCompare(b.name);
  if (sel.new === 'class') body.append(h('tr', null, h('td', { class: 'editor-cell', colspan: '4' }, classEditor({ name: '', parent: null, description: '', properties: [] }, o, true))));
  const walk = (c, depth) => {
    const open = sel.class === c.name;
    const children = (kids.get(c.name) || []).slice().sort(bySize);
    const own = classCount(c, cu), all = total.get(c.name);
    const rule = matchText(c.match);
    body.append(h('tr', { class: 'clickable', 'aria-selected': open ? 'true' : 'false', 'aria-level': String(depth + 1), tabindex: '0', onclick: () => patchQuery({ class: open ? '' : c.name, new: '' }), onkeydown: (ev) => { if (ev.key === 'Enter') patchQuery({ class: open ? '' : c.name }); } },
      h('td', null, h('div', { class: 'tree-name', style: `--depth:${depth}` },
        h('span', { class: 'swatch', style: `--swatch:${classColor(c.name)}` }),
        h('span', { class: 'stack tight' }, h('span', { text: c.name }), rule ? h('span', { class: 'hint mono', text: rule }) : null,
          (c.edges || []).map((e) => h('span', { class: 'hint', title: ruleText(e), text: edgeShapeText(e) }))))),
      h('td', { class: 'muted', text: ruleText(c) }),
      h('td', { class: 'num', title: children.length ? `${num(own)} direct, ${num(all)} with subclasses` : null }, num(own), children.length ? h('span', { class: 'hint block', text: `${num(all)} in total` }) : null),
      h('td', { class: 'num', text: num((c.properties || []).length) })));
    if (open) body.append(h('tr', null, h('td', { class: 'editor-cell', colspan: '4' }, classEditor(c, o, false))));
    for (const k of children) walk(k, depth + 1);
  };
  roots.slice().sort(bySize).forEach((c) => walk(c, 0));
  return h('section', { class: 'stack' }, h('h2', { text: 'Classes' }),
    o.classes.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'tree' }, h('thead', null, h('tr', null, h('th', { text: 'Class' }), h('th', { text: 'Origin' }), h('th', { class: 'num', text: 'Nodes' }), h('th', { class: 'num', text: 'Properties' }))), body))
      : h('div', { class: 'empty' }, h('p', { text: 'No classes yet. A class names a kind of node, such as Person or Service.' }), h('button', { 'data-write': true, onclick: () => patchQuery({ new: 'class' }) }, 'Add class')));
}

function classEditor(c, o, isNew) {
  const name = h('input', { value: c.name, 'aria-label': 'Class name', disabled: !isNew || S.readOnly, required: true });
  const parent = h('select', { 'aria-label': 'Parent class', disabled: S.readOnly }, h('option', { value: '', text: 'No parent' }), o.classes.filter((x) => x.name !== c.name).map((x) => h('option', { value: x.name, text: x.name, selected: x.name === c.parent })));
  const desc = h('input', { value: c.description || '', 'aria-label': 'Description', placeholder: 'What this class means', disabled: S.readOnly });
  const props = (c.properties || []).map((p) => ({ ...p }));
  const list = h('div', { class: 'stack' });
  const drawProps = () => {
    list.replaceChildren(...props.map((p, idx) => h('div', { class: 'prop-row' },
      h('input', { value: p.name, 'aria-label': 'Property name', disabled: S.readOnly, oninput: (ev) => { p.name = ev.target.value; } }),
      h('select', { 'aria-label': 'Property type', disabled: S.readOnly, onchange: (ev) => { p.type = ev.target.value; } }, ['string', 'integer', 'number', 'boolean', 'url', 'date', 'list', 'object'].map((t) => h('option', { value: t, text: t, selected: t === p.type }))),
      h('label', { class: 'row' }, h('input', { type: 'checkbox', checked: !!p.required, disabled: S.readOnly, onchange: (ev) => { p.required = ev.target.checked; } }), 'Required'),
      h('button', { type: 'button', class: 'quiet', 'data-write': true, onclick: () => { props.splice(idx, 1); drawProps(); } }, 'Remove'))));
    if (!props.length) list.append(h('p', { class: 'muted', text: 'No properties. A property is an attribute that nodes of this class carry.' }));
  };
  drawProps();
  return h('form', { class: 'stack', onsubmit: (ev) => { ev.preventDefault(); if (!name.value.trim()) { name.setAttribute('aria-invalid', 'true'); return; } saveClass({ name: name.value.trim(), parent: parent.value || null, description: desc.value, color: c.color || null, properties: props.filter((p) => p.name.trim()) }); } },
    h('div', { class: 'row' }, h('label', { class: 'field' }, h('span', { text: 'Name' }), name), h('label', { class: 'field' }, h('span', { text: 'Parent' }), parent)),
    h('label', { class: 'field' }, h('span', { text: 'Description' }), desc),
    h('h3', { text: 'Properties' }), list,
    h('div', { class: 'row', 'data-write': true },
      h('button', { type: 'button', onclick: () => { props.push({ name: '', type: 'string', required: false, description: '' }); drawProps(); } }, 'Add property'),
      h('button', { type: 'submit', class: 'primary' }, isNew ? 'Create class' : 'Save class'),
      isNew ? h('button', { type: 'button', class: 'quiet', onclick: () => patchQuery({ new: '' }) }, 'Cancel')
        : h('button', { type: 'button', class: 'danger', onclick: async () => { try { S.onto = await api('DELETE', `/api/ontology/classes/${enc(c.name)}`); S.report = null; undoable(`Removed class ${c.name}.`, S.onto); patchQuery({ class: '' }); rerender(); } catch (e) { fail(e); } } }, 'Remove class')));
}

function relationTable(o, ru, sel) {
  const body = h('tbody');
  const support = (r) => (typeof r.support === 'number' ? r.support : (ru.get(r.name) || 0));
  const rows = o.relations.slice().sort((a, b) => support(b) - support(a));
  if (sel.new === 'relation') body.append(h('tr', null, h('td', { class: 'editor-cell', colspan: '4' }, relationEditor({ name: '', domain: [], range: [], cardinality: 'many', description: '' }, o, true))));
  for (const r of rows) {
    const open = sel.relation === r.name;
    body.append(h('tr', { class: 'clickable', 'aria-selected': open ? 'true' : 'false', tabindex: '0', onclick: () => patchQuery({ relation: open ? '' : r.name, new: '' }), onkeydown: (ev) => { if (ev.key === 'Enter') patchQuery({ relation: open ? '' : r.name }); } },
      h('td', null, h('span', { class: 'stack tight' }, h('span', { text: r.name }), h('span', { class: 'hint', text: [ruleText(r), r.cardinality === 'one' ? 'at most one out' : '', r.in_cardinality === 'one' ? 'at most one in' : '', r.acyclic ? 'no cycles' : ''].filter(Boolean).join(', ') }))),
      h('td', { class: 'muted', text: (r.domain && r.domain.length ? r.domain.join(', ') : 'Any class') + ' to ' + (r.range && r.range.length ? r.range.join(', ') : 'any class') }),
      h('td', { class: 'num', text: typeof r.confidence === 'number' ? `${Math.round(r.confidence * 100)}%` : '' }),
      h('td', { class: 'num', text: num(typeof r.support === 'number' ? r.support : (ru.get(r.name) || 0)) })));
    if (open) body.append(h('tr', null, h('td', { class: 'editor-cell', colspan: '4' },
      r.domain_profile || r.range_profile ? h('dl', { class: 'kv' }, h('dt', { text: 'Seen from' }), h('dd', { text: profileText(r.domain_profile) || '—' }), h('dt', { text: 'Seen to' }), h('dd', { text: profileText(r.range_profile) || '—' })) : null,
      relationEditor(r, o, false))));
  }
  return h('section', { class: 'stack' }, h('h2', { text: 'Relations' }),
    rows.length ? h('div', { class: 'table-wrap' }, h('table', null, h('thead', null, h('tr', null, h('th', { text: 'Relation' }), h('th', { text: 'From and to' }), h('th', { class: 'num', title: 'Mean extraction confidence of its edges', text: 'Confidence' }), h('th', { class: 'num', text: 'Edges' }))), body))
      : h('div', { class: 'empty' }, h('p', { text: 'No relations yet. A relation names a kind of edge and the classes it may join.' }), h('button', { 'data-write': true, onclick: () => patchQuery({ new: 'relation' }) }, 'Add relation')));
}

function relationEditor(r, o, isNew) {
  const name = h('input', { value: r.name, 'aria-label': 'Relation name', disabled: !isNew || S.readOnly, required: true });
  const checks = (picked, label) => h('fieldset', { class: 'stack', style: 'border:0;padding:0;margin:0' }, h('legend', { class: 'muted', text: label }),
    h('div', { class: 'check-list' }, o.classes.map((c) => h('label', null, h('input', { type: 'checkbox', value: c.name, checked: (picked || []).includes(c.name), disabled: S.readOnly }), c.name))),
    h('p', { class: 'hint', text: 'No box means any class.' }));
  const dom = checks(r.domain, 'From classes'), rng = checks(r.range, 'To classes');
  const card = h('select', { 'aria-label': 'Edges per node', disabled: S.readOnly }, h('option', { value: 'many', text: 'Many per node', selected: r.cardinality !== 'one' }), h('option', { value: 'one', text: 'At most one per node', selected: r.cardinality === 'one' }));
  const sym = h('input', { type: 'checkbox', checked: !!r.symmetric, disabled: S.readOnly });
  const desc = h('input', { value: r.description || '', 'aria-label': 'Description', placeholder: 'What this relation means', disabled: S.readOnly });
  const picked = (fs) => $$('input:checked', fs).map((x) => x.value);
  return h('form', { class: 'stack', onsubmit: (ev) => { ev.preventDefault(); if (!name.value.trim()) { name.setAttribute('aria-invalid', 'true'); return; } saveRelation({ name: name.value.trim(), domain: picked(dom), range: picked(rng), cardinality: card.value, symmetric: sym.checked, inverse: r.inverse || null, description: desc.value }); } },
    h('div', { class: 'row' }, h('label', { class: 'field' }, h('span', { text: 'Name' }), name), h('label', { class: 'field' }, h('span', { text: 'Edges per node' }), card), h('label', { class: 'row' }, sym, 'Symmetric')),
    dom, rng, h('label', { class: 'field' }, h('span', { text: 'Description' }), desc),
    h('div', { class: 'row', 'data-write': true },
      h('button', { type: 'submit', class: 'primary' }, isNew ? 'Create relation' : 'Save relation'),
      isNew ? h('button', { type: 'button', class: 'quiet', onclick: () => patchQuery({ new: '' }) }, 'Cancel')
        : h('button', { type: 'button', class: 'danger', onclick: async () => { try { S.onto = await api('DELETE', `/api/ontology/relations/${enc(r.name)}`); S.report = null; undoable(`Removed relation ${r.name}.`, S.onto); patchQuery({ relation: '' }); rerender(); } catch (e) { fail(e); } } }, 'Remove relation')));
}

function drawOntologyMap(host, o, cu, ru) {
  const cv = h('canvas', { role: 'img', 'aria-label': `${plural(o.classes.length, 'class', 'classes')} and ${plural(o.relations.length, 'relation')}` });
  host.append(cv);
  const draw = () => {
    const r = host.getBoundingClientRect(); const dpr = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = r.width * dpr; cv.height = r.height * dpr;
    const ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const names = o.classes.map((c) => c.name);
    if (!names.length) { ctx.fillStyle = cssVar('--color-muted'); ctx.font = `14px ${cssVar('--font-body')}`; ctx.fillText('No classes yet.', 20, 30); return; }
    const W = r.width, H = r.height, cx = W / 2, cy = H / 2 + 6;
    // An ellipse that keeps every class name inside the frame.
    ctx.font = `500 13px ${cssVar('--font-body')}`;
    const half = Math.min(W / 4, Math.max(...names.map((n) => ctx.measureText(n).width)) / 2 + 8);
    const Rx = Math.max(20, W / 2 - half - 8), Ry = Math.max(20, H / 2 - 58);
    const pos = new Map();
    names.forEach((n, i) => { const a = (i / names.length) * Math.PI * 2 - Math.PI / 2; pos.set(n, names.length === 1 ? [cx, cy] : [cx + Math.cos(a) * Rx, cy + Math.sin(a) * Ry]); });
    const maxUse = Math.max(1, ...Array.from(ru.values()));
    // One curve per class pair and direction: parallel relations share it and its label.
    const pairs = new Map();
    for (const rel of o.relations) {
      const doms = rel.domain && rel.domain.length ? rel.domain : null, rngs = rel.range && rel.range.length ? rel.range : null;
      if (!doms || !rngs) continue;
      for (const a of doms) for (const b of rngs) {
        if (!pos.get(a) || !pos.get(b)) continue;
        const k = a + '\u0000' + b; let e = pairs.get(k); if (!e) pairs.set(k, (e = { a, b, rels: [] }));
        e.rels.push(rel.name);
      }
    }
    const label = (text, x, y) => {
      ctx.font = `12px ${cssVar('--font-body')}`; const w = ctx.measureText(text).width + 10;
      ctx.fillStyle = cssVar('--color-paper'); ctx.globalAlpha = 0.9; ctx.fillRect(x - w / 2, y - 9, w, 18); ctx.globalAlpha = 1;
      ctx.fillStyle = cssVar('--color-muted'); ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(text, x, y); ctx.textBaseline = 'alphabetic';
    };
    const pairLabel = (rels) => { const s = rels.slice().sort((x, y) => (ru.get(y) || 0) - (ru.get(x) || 0)); return s.length > 2 ? `${s.slice(0, 2).join(', ')} +${s.length - 2}` : s.join(', '); };
    ctx.lineCap = 'round';
    const labels = [];
    for (const { a, b, rels } of pairs.values()) {
      const p = pos.get(a), q = pos.get(b);
      const use = rels.reduce((s, n) => s + (ru.get(n) || 0), 0);
      ctx.strokeStyle = cssVar('--color-rule-2'); ctx.globalAlpha = 0.85; ctx.lineWidth = 1 + 3 * Math.sqrt(Math.min(1, use / maxUse));
      ctx.beginPath();
      if (a === b) { ctx.arc(p[0], p[1] - 26, 16, 0, Math.PI * 2); ctx.stroke(); labels.push([pairLabel(rels), p[0], p[1] - 52]); continue; }
      const bend = pairs.has(b + '\u0000' + a) ? 0.3 : 0.15;
      const mx = (p[0] + q[0]) / 2 + (q[1] - p[1]) * bend, my = (p[1] + q[1]) / 2 - (q[0] - p[0]) * bend;
      ctx.moveTo(p[0], p[1]); ctx.quadraticCurveTo(mx, my, q[0], q[1]); ctx.stroke();
      const ang = Math.atan2(q[1] - my, q[0] - mx); const ex = q[0] - Math.cos(ang) * 16, ey = q[1] - Math.sin(ang) * 16;
      ctx.fillStyle = cssVar('--color-rule-2'); ctx.beginPath(); ctx.moveTo(ex, ey); ctx.lineTo(ex - Math.cos(ang - 0.4) * 9, ey - Math.sin(ang - 0.4) * 9); ctx.lineTo(ex - Math.cos(ang + 0.4) * 9, ey - Math.sin(ang + 0.4) * 9); ctx.fill();
      // The curve passes through the midpoint of the control point and the chord.
      labels.push([pairLabel(rels), ((p[0] + q[0]) / 2 + mx) / 2, ((p[1] + q[1]) / 2 + my) / 2]);
    }
    ctx.globalAlpha = 1;
    // Greedy label placement by priority: discs first, then class labels by size,
    // then relation labels. A label that would cover another one is left out;
    // the table below the map lists every class and relation.
    const placed = [];
    const hits = (x, y, w, hh) => placed.some((b) => Math.abs(b.x - x) < (b.w + w) / 2 && Math.abs(b.y - y) < (b.h + hh) / 2);
    const maxN = Math.max(1, ...names.map((n) => cu.get(n) || 0));
    const radius = (n) => 6 + 14 * Math.sqrt((cu.get(n) || 0) / maxN);
    for (const n of names) {
      const [x, y] = pos.get(n); const rad = radius(n);
      const spec = o.classes.find((c) => c.name === n);
      if (spec && spec.parent && pos.get(spec.parent)) { const [px, py] = pos.get(spec.parent); ctx.setLineDash([3, 4]); ctx.strokeStyle = cssVar('--color-muted'); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(px, py); ctx.stroke(); ctx.setLineDash([]); }
      ctx.fillStyle = classColor(n); ctx.beginPath(); ctx.arc(x, y, rad, 0, Math.PI * 2); ctx.fill();
      placed.push({ x, y, w: rad * 2, h: rad * 2 });
    }
    ctx.textAlign = 'center';
    for (const n of names.slice().sort((a, b) => (cu.get(b) || 0) - (cu.get(a) || 0))) {
      const [x, y] = pos.get(n); const rad = radius(n);
      ctx.font = `500 13px ${cssVar('--font-body')}`; const w = ctx.measureText(n).width + 6;
      const ly = y + rad + 16, cyBox = ly + 3;
      if (hits(x, cyBox, w, 32)) continue;
      placed.push({ x, y: cyBox, w, h: 32 });
      // A halo in the paper colour keeps the text readable over the curves.
      ctx.lineJoin = 'round'; ctx.lineWidth = 4; ctx.strokeStyle = cssVar('--color-paper');
      ctx.strokeText(n, x, ly); ctx.fillStyle = cssVar('--color-ink'); ctx.fillText(n, x, ly);
      ctx.font = `12px ${cssVar('--font-outlier')}`; const count = num(cu.get(n) || 0);
      ctx.strokeText(count, x, ly + 15); ctx.fillStyle = cssVar('--color-muted'); ctx.fillText(count, x, ly + 15);
    }
    for (const [t, x, y0] of labels) {
      ctx.font = `12px ${cssVar('--font-body')}`; const w = ctx.measureText(t).width + 10;
      const y = [y0, y0 - 20, y0 + 20].find((yy) => !hits(x, yy, w, 18));
      if (y === undefined) continue;
      placed.push({ x, y, w, h: 18 }); label(t, x, y);
    }
  };
  requestAnimationFrame(draw);
  new ResizeObserver(() => draw()).observe(host);
}

// ------------------------------------------------------------ findings view
async function renderFindingsView(root) {
  const page = h('div', { class: 'page' }); const inner = h('div', { class: 'page-inner' }); page.append(inner); root.append(page);
  const rerun = async () => { S.report = null; rerender(); };
  inner.append(h('div', { class: 'page-head' }, h('div', null, h('h1', { text: 'Findings' }), h('p', { text: 'Where the graph agrees with the ontology, and where it does not.' })), STATIC ? null : h('button', { onclick: rerun }, 'Check again')));
  const body = h('div', { class: 'stack' }); inner.append(body);
  if (!S.report) { body.append(skeletonState('Checking the graph')); try { await loadReport(); } catch (e) { body.replaceChildren(failedState(e, rerun)); return; } }
  const r = S.report;
  const sev = S.route.q.severity || '';
  const code = S.route.q.code || '';
  body.replaceChildren(
    h('div', { class: 'figures' },
      h('div', { dataset: { sev: 'error' } }, h('span', { class: 'figure', text: num(r.counts.error) }), h('span', { text: 'Errors' })),
      h('div', { dataset: { sev: 'warning' } }, h('span', { class: 'figure', text: num(r.counts.warning) }), h('span', { text: 'Warnings' })),
      h('div', { dataset: { sev: 'info' } }, h('span', { class: 'figure', text: num(r.counts.info) }), h('span', { text: 'Notes' })),
      h('div', null, h('span', { class: 'figure', text: num(r.nodes) }), h('span', { text: `nodes and ${num(r.edges)} edges checked` }))));
  if (!r.total) { body.append(h('div', { class: 'table-wrap' }, h('div', { class: 'empty' }, h('h2', { text: 'No findings' }), h('p', { text: 'Every node and edge fits the ontology.' }), h('button', { onclick: () => setRoute('ontology') }, 'Open the ontology')))); return; }
  const tabs = h('div', { class: 'segmented', role: 'group', 'aria-label': 'Severity' }, [['', 'All'], ['error', 'Errors'], ['warning', 'Warnings'], ['info', 'Notes']].map(([v, l]) => h('button', { 'aria-pressed': sev === v ? 'true' : 'false', onclick: () => patchQuery({ severity: v, page: '' }) }, l)));
  const codes = h('div', { class: 'row' }, Object.entries(r.by_code).map(([c, n]) => h('button', { class: 'chip', 'aria-pressed': code === c ? 'true' : 'false', onclick: () => patchQuery({ code: code === c ? '' : c, page: '' }) }, findingLabel(c), ' ', h('span', { class: 'figure', text: num(n) }))));
  const rows = r.findings.filter((f) => (!sev || f.severity === sev) && (!code || f.code === code));
  const pageNo = Math.max(0, parseInt(S.route.q.page || '0', 10));
  const per = 100;
  const tbody = h('tbody');
  // Show labels, keep the raw id in the tooltip.
  const nameOf = (id) => (graph.index.has(id) ? graph.labels[graph.index.get(id)] : id);
  for (const f of rows.slice(pageNo * per, pageNo * per + per)) {
    const subject = f.node || (f.edge && f.edge.source) || '';
    const subjectIds = f.node || (f.edge ? `${f.edge.source} → ${f.edge.target}` : '') || f.ontology_class || f.ontology_relation || '';
    const subjectText = f.node ? nameOf(f.node) : f.edge ? `${nameOf(f.edge.source)} → ${nameOf(f.edge.target)}` : subjectIds;
    tbody.append(h('tr', { class: subject ? 'clickable' : '', tabindex: subject ? '0' : null, onclick: () => subject && setRoute('graph', subject), onkeydown: (ev) => { if (ev.key === 'Enter' && subject) setRoute('graph', subject); } },
      h('td', null, h('span', { class: 'badge', dataset: { sev: f.severity }, text: f.severity === 'info' ? 'note' : f.severity })),
      h('td', { class: 'check', title: f.code, text: findingLabel(f.code) }),
      h('td', { text: f.message }),
      h('td', { class: 'subject muted', title: subjectIds }, breakable(subjectText))));
  }
  body.append(h('div', { class: 'row spread' }, tabs, h('p', { class: 'muted', text: r.truncated ? `Showing the first ${num(r.findings.length)} of ${num(r.total)} findings.` : `${plural(rows.length, 'finding')}` })), codes,
    rows.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'findings' }, h('thead', null, h('tr', null, h('th', { text: 'Severity' }), h('th', { text: 'Check' }), h('th', { text: 'Finding' }), h('th', { text: 'Subject' }))), tbody))
      : h('div', { class: 'empty' }, h('p', { text: 'Nothing matches this filter.' }), h('button', { onclick: () => patchQuery({ severity: '', code: '' }) }, 'Clear the filter')),
    rows.length > per ? h('div', { class: 'row' }, h('button', { disabled: pageNo === 0, onclick: () => patchQuery({ page: String(pageNo - 1) }) }, 'Previous'), h('span', { class: 'muted figure', text: `${pageNo + 1} / ${Math.ceil(rows.length / per)}` }), h('button', { disabled: (pageNo + 1) * per >= rows.length, onclick: () => patchQuery({ page: String(pageNo + 1) }) }, 'Next')) : null);
}

// ---------------------------------------------------------------- markdown
function escapeHtml(s) { return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function assetUrl(docId, name) {
  return STATIC ? `${STATIC.asset_base || 'kb/documents/'}${enc(docId)}/assets/${enc(name)}` : `/api/documents/${enc(docId)}/assets/${enc(name)}`;
}
function safeHref(href, docId) {
  const t = href.trim();
  if (/^assets\//.test(t)) return assetUrl(docId, t.slice(7));
  // A bare file name points at a file of this document.
  if (/^[\w][\w.-]*$/.test(t) && t.includes('.')) return assetUrl(docId, t);
  if (/^(https?:|mailto:|#)/i.test(t)) return t;
  if (/^[\w./-]+$/.test(t) && !t.includes('..')) return t;
  return '#';
}
function inline(text, docId) {
  let s = escapeHtml(text);
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_, alt, src) => `<img alt="${alt}" src="${escapeHtml(safeHref(src.replace(/&amp;/g, '&'), docId))}" loading="lazy">`);
  s = s.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_, label) => `<a href="#" class="wiki link" data-wiki="${label.trim()}">${label.trim()}</a>`);
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, t, href) => `<a class="link" href="${escapeHtml(safeHref(href.replace(/&amp;/g, '&'), docId))}" rel="noopener noreferrer" target="_blank">${t}</a>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
  s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
  return s;
}
function markdown(src, docId) {
  const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
  const out = []; let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; out.push(`<pre><code>${escapeHtml(buf.join('\n'))}</code></pre>`); continue;
    }
    const hm = /^(#{1,6})\s+(.*)$/.exec(line);
    if (hm) { const lvl = Math.min(hm[1].length + 1, 4); out.push(`<h${lvl}>${inline(hm[2], docId)}</h${lvl}>`); i++; continue; }
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) { out.push('<hr>'); i++; continue; }
    if (/^>\s?/.test(line)) { const buf = []; while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, '')); out.push(`<blockquote>${inline(buf.join(' '), docId)}</blockquote>`); continue; }
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line); const buf = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) buf.push(lines[i++].replace(/^\s*([-*+]|\d+\.)\s+/, ''));
      out.push(`<${ordered ? 'ol' : 'ul'}>${buf.map((b) => `<li>${inline(b, docId)}</li>`).join('')}</${ordered ? 'ol' : 'ul'}>`); continue;
    }
    if (/^\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      const head = cells(line); i += 2; const rows = [];
      while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push(`<div class="table-wrap"><table><thead><tr>${head.map((c) => `<th>${inline(c, docId)}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c, docId)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`); continue;
    }
    if (!line.trim()) { i++; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|>|\s*([-*+]|\d+\.)\s+|\|)/.test(lines[i])) buf.push(lines[i++]);
    if (!buf.length) buf.push(lines[i++]);
    out.push(`<p>${inline(buf.join(' '), docId)}</p>`);
  }
  return out.join('\n');
}

// --------------------------------------------------------------- documents
async function renderDocumentsView(root) {
  const left = h('aside', { class: 'pane left', 'aria-label': 'Documents' });
  const right = h('section', { class: 'pane', id: 'doc-pane', 'aria-label': 'Document' });
  root.append(h('div', { class: 'view docs', dataset: { pane: S.route.arg || S.route.q.ingest ? 'detail' : 'list' } }, left, right));
  left.append(h('div', { class: 'row spread' }, h('h1', { style: 'font-size:var(--text-lg)', text: 'Documents' }),
    h('div', { class: 'row', 'data-write': true }, h('button', { onclick: () => setRoute('documents', '', { ingest: '1' }) }, 'Ingest'), h('label', { class: 'row', for: 'new-doc-link' }, h('input', { type: 'checkbox', id: 'new-doc-link', checked: true }), 'Link mentions'), h('button', { class: 'primary', onclick: () => newDocument({}) }, 'New document'))));
  const filter = h('input', { type: 'search', placeholder: 'Filter by title or tag', 'aria-label': 'Filter documents', value: S.route.q.filter || '' });
  left.append(h('div', { class: 'stack', style: 'margin-top:var(--space-sm)' }, filter));
  const listHost = h('div', { style: 'margin-top:var(--space-sm)' }); left.append(listHost);
  if (!S.docs) { listHost.append(skeletonState('Loading documents')); try { await loadDocs(); } catch (e) { listHost.replaceChildren(failedState(e, rerender)); return; } }
  const drawList = () => {
    const f = filter.value.trim().toLowerCase();
    const docs = S.docs.filter((d) => !f || String(d.title).toLowerCase().includes(f) || (d.tags || []).some((t) => t.toLowerCase().includes(f)));
    if (!S.docs.length) { listHost.replaceChildren(h('div', { class: 'empty' }, h('p', { text: 'No documents yet. A document is a markdown box with images, PDFs and links to nodes.' }), S.readOnly ? null : h('button', { class: 'primary', onclick: () => newDocument({}) }, 'Write the first document'))); return; }
    const tbody = h('tbody');
    for (const d of docs) {
      tbody.append(h('tr', { class: 'clickable', tabindex: '0', 'aria-selected': S.route.arg === d.id ? 'true' : 'false', onclick: () => setRoute('documents', d.id), onkeydown: (ev) => { if (ev.key === 'Enter') setRoute('documents', d.id); } },
        h('td', null, h('div', { class: 'doc-title' }, h('span', { text: d.title }), d.review ? h('span', { class: 'badge', dataset: { sev: 'warning' }, title: `${plural(d.review, 'open link')} to review`, text: `Review ${num(d.review)}` }) : null), d.tags && d.tags.length ? h('div', { class: 'hint', text: d.tags.join(', ') }) : null),
        h('td', { class: 'num', text: num((d.assets || []).length) }),
        h('td', { class: 'mono muted', text: when(d.updated_at).slice(0, 10) })));
    }
    listHost.replaceChildren(docs.length ? h('div', { class: 'table-wrap' }, h('table', null, h('thead', null, h('tr', null, h('th', { text: 'Title' }), h('th', { class: 'num', text: 'Files' }), h('th', { text: 'Updated' }))), tbody)) : h('p', { class: 'muted', text: 'No document matches the filter.' }));
  };
  filter.addEventListener('input', debounce(drawList, 120));
  drawList();
  if (S.route.q.ingest && !S.readOnly) renderIngest(right);
  else if (S.route.arg) renderDocument(right, S.route.arg);
  else right.append(h('div', { class: 'empty' }, h('h2', { text: 'Pick a document' }), h('p', { text: 'Documents hold notes, images, PDFs and other files, and link to nodes in the graph. Write [[Label]] in a document to link a node.' })));
}

async function newDocument({ title, nodes }) {
  // The same switch as Ingest: link names in the body to known nodes.
  const box = $('#new-doc-link');
  try {
    const doc = await api('POST', '/api/documents', { title: title || 'Untitled document', body: '', nodes: nodes || [], link: box ? box.checked : true });
    S.docs = null; await refreshAll();
    undoable(`Created ${doc.title}.`, doc);
    setRoute('documents', doc.id, { edit: '1' });
  } catch (e) { fail(e); }
}

// ------------------------------------------------------------------ review
// Plain words for each reason that the linker or the LLM gives (kb_link, kb_ingest).
const REVIEW_REASON = {
  low_margin: 'Several nodes fit almost equally', no_context: 'The text gives no clue',
  weak_name: 'A weak name, and the text does not support it', ambiguous_context: 'Nearby words point to different nodes',
  ambiguous_wiki: 'The wiki link fits several nodes', test_file: 'The only match is in a test file',
  same_file_name: 'Several files have this name', possible_match: 'A near spelling of an existing node',
  possible_same_as: 'A concept with the name of a code node',
};
// About 60 characters of the body around the mention, with the mention marked.
function reviewContext(body, p) {
  const text = String(p.mention || '');
  let at = Number.isInteger(p.offset) && body.slice(p.offset, p.offset + text.length) === text ? p.offset : body.indexOf(text);
  if (at < 0 || !text) return h('p', { class: 'review-context' }, h('mark', { text }));
  const a = Math.max(0, at - 30), b = Math.min(body.length, at + text.length + 30);
  const flat = (s) => s.replace(/\s+/g, ' ');
  return h('p', { class: 'review-context' }, (a > 0 ? '…' : '') + flat(body.slice(a, at)), h('mark', { text }), flat(body.slice(at + text.length, b)) + (b < body.length ? '…' : ''));
}
// One row for each open "possible" item. A decision posts to the review route
// (Link: the node; Not a link: null) and offers Undo by history seq.
function reviewSection(doc, id, pane) {
  const items = doc.possible || [];
  if (!items.length) return null;
  const body = doc.body || '';
  // The 200 answer is the document: draw it at once, then refresh the rest.
  const decide = async (row, mention, node, label) => {
    const buttons = $$('button', row);
    for (const b of buttons) b.disabled = true;
    try {
      const r = await api('POST', `/api/documents/${enc(id)}/review`, { mention, node });
      S.docs = null;
      renderDocument(pane, id, r);
      undoable(node ? `Linked ${mention} to ${label}.` : `${mention} is not a link.`, r);
      await refreshAll();
    } catch (e) {
      if (e.status === 400 && /no open review item/i.test(e.message)) { toast('This name is already decided.'); renderDocument(pane, id); return; }
      for (const b of buttons) b.disabled = false;
      fail(e);
    }
  };
  const rows = items.map((p) => {
    const cands = p.candidates || [];
    const more = (p.candidates_total || cands.length) - cands.length;
    const row = h('li', { class: 'review-item' },
      reviewContext(body, p),
      h('p', { class: 'hint', text: REVIEW_REASON[p.reason] || String(p.reason || '').replace(/_/g, ' ') }),
      h('ul', { class: 'review-cands' }, cands.map((c) => h('li', null,
        h('div', { class: 'review-cand' },
          h('button', { class: 'link', title: c.id, onclick: () => setRoute('graph', c.id) }, breakable(c.label || c.id)),
          h('span', { class: 'hint mono', text: [c.source_file ? shortName(c.source_file, 48) : '', typeof c.score === 'number' ? c.score.toFixed(2) : ''].filter(Boolean).join(' · ') })),
        h('button', { 'data-write': true, 'aria-label': `Link ${p.mention} to ${c.label || c.id}`, onclick: () => decide(row, p.mention, c.id, c.label || c.id) }, 'Link')))),
      more > 0 ? h('p', { class: 'hint', text: `${plural(more, 'other candidate')} not shown.` }) : null,
      h('div', { class: 'row', 'data-write': true }, h('button', { class: 'quiet', 'aria-label': `${p.mention} is not a link`, onclick: () => decide(row, p.mention, null) }, 'Not a link')));
    return row;
  });
  return h('section', { class: 'stack review' }, h('h2', null, 'Review ', h('span', { class: 'badge', dataset: { sev: 'warning' }, text: num(items.length) })), h('ul', { class: 'review-list' }, rows));
}

// Break a file or code name after / _ - . and spaces, not inside a word.
// One span holds the parts, so a flex parent gets one item, not one item for each part.
function breakable(name) {
  return h('span', null, String(name).split(/(?<=[/_.\- ])/).flatMap((part, i) => (i ? [h('wbr'), part] : [part])));
}

// "Open this document" on a document node, with its open review count.
function docOpenButton(docId) {
  const b = h('button', { onclick: () => setRoute('documents', docId) }, 'Open this document');
  const fill = () => {
    const d = (S.docs || []).find((x) => x.id === docId);
    if (d && d.review) b.append(' ', h('span', { class: 'badge', dataset: { sev: 'warning' }, text: `Review ${num(d.review)}` }));
  };
  if (S.docs) fill(); else if (!STATIC) loadDocs().then(fill, () => {});
  return b;
}

async function renderDocument(pane, id, given) {
  if (!given) pane.replaceChildren(skeletonState('Loading the document'));
  let doc = given;
  if (!doc) try {
    doc = STATIC ? STATIC.documents.find((d) => d.id === id) : await api('GET', `/api/documents/${enc(id)}`);
    if (!doc) throw new Error(`Document ${id} does not exist.`);
  } catch (e) { pane.replaceChildren(failedState(e, () => renderDocument(pane, id))); return; }
  const editing = S.route.q.edit === '1' && !S.readOnly;
  const save = async (fields, message) => {
    try { const r = await api('PATCH', `/api/documents/${enc(id)}`, fields); S.docs = null; await refreshAll(); undoable(message || 'Saved the document.', r); } catch (e) { fail(e); }
  };
  const head = h('div', { class: 'page-head' },
    h('div', { class: 'stack' }, docTitle(doc, (v) => save({ title: v }, `Renamed to ${v}.`)),
      h('p', { class: 'row' }, h('span', { class: 'muted', text: 'Tags' }), editableValue((doc.tags || []).join(', '), (v) => save({ tags: v.split(',').map((t) => t.trim()).filter(Boolean) }, 'Saved the tags.'), { placeholder: 'No tags', label: 'Tags', alwaysShow: true })),
      doc.source ? h('p', { class: 'hint mono', text: `Source ${doc.source}` }) : null),
    h('div', { class: 'row' },
      h('div', { class: 'segmented', role: 'group', 'aria-label': 'Mode', 'data-write': true },
        h('button', { 'aria-pressed': editing ? 'false' : 'true', onclick: () => setRoute('documents', id, {}) }, 'Read'),
        h('button', { 'aria-pressed': editing ? 'true' : 'false', onclick: () => setRoute('documents', id, { edit: '1' }) }, 'Edit')),
      doc.node_id ? h('button', { onclick: () => setRoute('graph', doc.node_id) }, 'Show on the map') : null));
  let body;
  if (editing) {
    const ta = h('textarea', { class: 'code', 'aria-label': 'Markdown', spellcheck: 'true' }, doc.body || '');
    body = h('form', { class: 'stack', onsubmit: async (ev) => { ev.preventDefault(); await save({ body: ta.value }, 'Saved the document.'); setRoute('documents', id, {}); } },
      ta, h('p', { class: 'hint', text: 'Markdown. Write [[Label]] to link a node; a new label creates a concept node, [[Label|Class]] picks its class. Reference a file as assets/name.png.' }),
      h('div', { class: 'row' }, h('button', { type: 'submit', class: 'primary' }, 'Save'), h('button', { type: 'button', class: 'quiet', onclick: () => setRoute('documents', id, {}) }, 'Cancel')));
    setTimeout(() => ta.focus(), 0);
  } else {
    body = h('article', { class: 'doc-body' });
    body.innerHTML = doc.body && doc.body.trim() ? markdown(doc.body, id) : '<p class="muted">This document is empty.</p>';
    body.addEventListener('click', (ev) => {
      const a = ev.target.closest('a.wiki'); if (!a) return;
      ev.preventDefault();
      const label = a.dataset.wiki.toLowerCase();
      const i = graph.labels.findIndex((l) => l.toLowerCase() === label);
      if (i >= 0) setRoute('graph', graph.ids[i]); else toast(`No node has the label ${a.dataset.wiki}.`);
    });
  }
  const linked = h('section', { class: 'stack' }, h('h2', { text: 'Linked nodes' }),
    (doc.linked || []).length ? h('div', { class: 'row' }, doc.linked.map((l) => h('button', { class: 'chip', onclick: () => setRoute('graph', l.id), disabled: !l.exists }, l.label))) : h('p', { class: 'muted', text: 'No linked nodes. Mention a node with [[Label]], or ingest text that names nodes.' }));
  const assets = h('section', { class: 'stack' }, h('h2', { text: 'Files' }));
  if ((doc.assets || []).length) {
    const tb = h('tbody');
    for (const a of doc.assets) {
      tb.append(h('tr', null,
        h('td', { class: 'file-cell' }, h('a', { class: 'link file-link', href: assetUrl(id, a.name), target: '_blank', rel: 'noopener' }, breakable(a.name))),
        h('td', { class: 'muted', text: a.mime }),
        h('td', { class: 'num', text: bytes(a.size) }),
        h('td', { 'data-write': true }, h('button', { class: 'quiet', onclick: async () => { try { const r = await api('DELETE', `/api/documents/${enc(id)}/assets/${enc(a.name)}`); S.docs = null; undoable(`Removed ${a.name}.`, r); renderDocument(pane, id); } catch (e) { fail(e); } } }, 'Remove'))));
    }
    assets.append(h('div', { class: 'table-wrap' }, h('table', { class: 'files' }, h('thead', null, h('tr', null, h('th', { text: 'Name' }), h('th', { text: 'Type' }), h('th', { class: 'num', text: 'Size' }), h('th', { 'data-write': true }))), tb)));
    const images = doc.assets.filter((a) => (a.mime || '').startsWith('image/'));
    if (images.length && !images.some((a) => (doc.body || '').includes(a.name))) assets.append(h('div', { class: 'row' }, images.slice(0, 6).map((a) => h('img', { src: assetUrl(id, a.name), alt: a.name, style: 'max-height:140px;width:auto;border-radius:var(--radius-pill)' }))));
  } else assets.append(h('p', { class: 'muted', text: 'No files. Attach images, PDFs or any other file.' }));
  if (!S.readOnly) assets.append(dropzone(async (files) => {
    const made = [];
    for (const f of files) {
      try { made.push(await api('POST', `/api/documents/${enc(id)}/assets?name=${enc(f.name)}`, f, { headers: { 'Content-Type': f.type || 'application/octet-stream' } })); } catch (e) { fail(e); return; }
    }
    S.docs = null; undoable(`Attached ${plural(files.length, 'file')}.`, made); renderDocument(pane, id);
  }, 'Images, PDFs or any other file, up to 64 MB each.'));
  const danger = h('section', { 'data-write': true }, h('button', { class: 'danger', onclick: async () => {
    try { const r = await api('DELETE', `/api/documents/${enc(id)}`); S.docs = null; await refreshAll(); undoable(`Deleted ${doc.title}.`, r); setRoute('documents'); } catch (e) { fail(e); }
  } }, 'Delete document'));
  const back = h('button', { class: 'quiet back', type: 'button', onclick: () => setRoute('documents') }, 'All documents');
  pane.replaceChildren(h('div', { class: 'page-inner', style: 'padding:0' }, back, head, reviewSection(doc, id, pane), body, linked, assets, danger));
}

// A plain heading with a Rename button: the title stays a real h1 for
// assistive technology, and editing opens a labelled field.
function docTitle(doc, onSave) {
  const title = h('h1', { class: 'doc-title', text: doc.title });
  if (S.readOnly) return title;
  const wrap = h('div', { class: 'row title-row' });
  const edit = () => {
    const input = h('input', { value: doc.title, 'aria-label': 'Title', class: 'title-input' });
    const done = async (keep) => {
      const v = input.value.trim();
      if (keep && v && v !== doc.title) await onSave(v);
      else wrap.replaceChildren(title, button);
    };
    input.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); done(true); } else if (ev.key === 'Escape') done(false); });
    input.addEventListener('blur', () => done(true));
    wrap.replaceChildren(input); input.focus(); input.select();
  };
  const button = h('button', { class: 'quiet', type: 'button', onclick: edit }, 'Rename');
  wrap.append(title, button);
  return wrap;
}

const INGEST_ACCEPT = '.md,.markdown,.txt,.rst,.html,.htm,.pdf,.docx,.png,.jpg,.jpeg,.gif,.webp,.svg,.avif,.bash,.bat,.c,.cc,.cjs,.clj,.cljc,.cljs,.cmd,.cpp,.cs,.edn,.erl,.ex,.exs,.fish,.go,.h,.hpp,.hs,.java,.js,.jsx,.kt,.lua,.m,.mjs,.php,.pl,.ps1,.py,.r,.rb,.rs,.scala,.sh,.sql,.swift,.ts,.tsx,.zsh,text/*,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,image/*,image/avif';
// The server keeps a file up to 64 MB (kb.MAX_ASSET_BYTES). Check before the upload.
const INGEST_MAX_BYTES = 64 * 1024 * 1024;
let dropSeq = 0;
function dropzone(onFiles, label, accept) {
  const id = `drop-${++dropSeq}`;
  const input = h('input', { type: 'file', multiple: true, accept, id, class: 'visually-hidden' });
  const zone = h('div', { class: 'dropzone' },
    h('label', { class: 'button', for: id }, 'Choose files'), input,
    h('p', { class: 'hint', text: label }), h('p', { class: 'hint drop-hint', text: 'Or drop files here.' }));
  input.addEventListener('change', () => { if (input.files.length) onFiles(Array.from(input.files)); input.value = ''; });
  zone.addEventListener('dragover', (ev) => { ev.preventDefault(); zone.dataset.over = 'true'; });
  zone.addEventListener('dragleave', () => { zone.dataset.over = 'false'; });
  zone.addEventListener('drop', (ev) => { ev.preventDefault(); zone.dataset.over = 'false'; if (ev.dataTransfer.files.length) onFiles(Array.from(ev.dataTransfer.files)); });
  return zone;
}
// Upload one file with progress. fetch() has no upload progress, so this uses
// XMLHttpRequest with the same header and errors as api().
function uploadFile(path, file, onProgress) {
  if (STATIC) return Promise.reject(new ApiError(0, 'This is a read-only copy.'));
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open('POST', path);
    x.setRequestHeader('X-Graphify-KB', '1');
    x.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
    x.upload.onprogress = (ev) => { if (ev.lengthComputable) onProgress(ev.loaded / ev.total); };
    x.onload = () => {
      let data = null;
      try { data = x.responseText ? JSON.parse(x.responseText) : null; } catch { data = { error: x.responseText }; }
      if (x.status === 401) { showSignIn(); reject(new ApiError(401, 'A token is required.')); }
      else if (x.status < 200 || x.status >= 300) reject(new ApiError(x.status, (data && data.error) || x.statusText));
      else resolve(withChanges(data, x.getResponseHeader(CHANGE_HEADER)));
    };
    x.onerror = () => reject(new ApiError(0, 'The server did not answer.'));
    x.send(file);
  });
}
// A clear message for each ingest failure, by HTTP status.
function ingestError(e) {
  const detail = e && e.message ? e.message : String(e);
  const lead = { 400: 'The server refused the input.', 413: 'The file is too large.', 415: 'This file type is not supported. Use markdown, text, code, HTML, PDF, DOCX or an image.', 502: 'The LLM backend or the web page did not answer.', 0: 'The server did not answer. Check that graphify kb serve still runs.' }[e && e.status];
  // The server text for 415 already lists the types; do not repeat them.
  if (e && e.status === 415 && /not supported/i.test(detail)) return detail;
  return lead ? `${lead} ${detail}` : detail;
}

// Ingest state lives outside the pane. A live update can redraw the pane
// during an upload; the new pane paints the same state.
let ingestState = null; // { busy, label, share, made, error }
let ingestPaint = () => {};
function setIngest(next) { ingestState = next && { ...(ingestState || {}), ...next }; ingestPaint(); }
function renderIngest(pane) {
  const mode = h('select', { id: 'ingest-mode', 'aria-label': 'Extraction' }, h('option', { value: 'text', text: 'Match node labels (no LLM)' }), h('option', { value: 'llm', text: 'Extract nodes and edges with an LLM' }));
  const title = h('input', { id: 'ingest-title', placeholder: 'From the first heading or the file name' });
  const tags = h('input', { id: 'ingest-tags', placeholder: 'Comma separated' });
  const backend = h('input', { id: 'ingest-backend', placeholder: 'Automatic', autocomplete: 'off' });
  const model = h('input', { id: 'ingest-model', placeholder: 'Backend default', autocomplete: 'off' });
  const link = h('input', { type: 'checkbox', id: 'ingest-link', checked: true });
  const url = h('input', { id: 'ingest-url', type: 'url', placeholder: 'https://example.com/page', inputmode: 'url' });
  const text = h('textarea', { id: 'ingest-text', class: 'code', style: 'min-height:160px', placeholder: 'Paste markdown or plain text' });
  const bar = h('progress', { max: '1', 'aria-label': 'Upload progress' });
  const status = h('p', { class: 'muted' });
  const progress = h('div', { class: 'stack tight ingest-progress', hidden: true }, status, bar);
  const result = h('div', { class: 'stack', 'aria-live': 'polite' });
  const llmBox = h('div', { class: 'row fields', hidden: true },
    h('label', { class: 'field', for: 'ingest-backend' }, h('span', { text: 'Backend' }), backend),
    h('label', { class: 'field', for: 'ingest-model' }, h('span', { text: 'Model' }), model),
    h('p', { class: 'hint', style: 'flex-basis:100%', text: 'LLM extraction uses the backend that graphify finds in your environment, such as ANTHROPIC_API_KEY or GEMINI_API_KEY, and costs tokens.' }));
  mode.addEventListener('change', () => { llmBox.hidden = mode.value !== 'llm'; });
  const params = () => {
    const p = { mode: mode.value, title: title.value.trim() || undefined, tags: tags.value.split(',').map((t) => t.trim()).filter(Boolean), link: link.checked };
    if (p.mode === 'llm') { if (backend.value.trim()) p.backend = backend.value.trim(); if (model.value.trim()) p.model = model.value.trim(); }
    return p;
  };
  const report = (r) => h('p', null, `Created ${r.document.title}. `, r.linked ? `Linked ${plural(r.linked, 'node')} by name. ` : '', r.nodes_added && r.nodes_added.length ? `The LLM added ${plural(r.nodes_added.length, 'node')} and ${plural(r.edges_added, 'edge')}. ` : '');
  const paint = () => {
    const st = ingestState || {};
    $$('button, input, select, textarea', pane).forEach((x) => { x.disabled = !!st.busy; });
    progress.hidden = !st.busy;
    status.textContent = st.label || '';
    if (typeof st.share === 'number' && st.share < 1) bar.value = st.share; else bar.removeAttribute('value');
    if (st.busy) result.replaceChildren();
    else if (st.error) result.replaceChildren(h('p', { class: 'error-text', role: 'alert', text: st.error }));
    else if (st.made && st.made.length) {
      const made = st.made, last = made[made.length - 1];
      result.replaceChildren(...made.map(report), ...made.flatMap((r) => (r.notes || []).map((n) => h('p', { class: 'hint', text: n }))), h('button', { onclick: () => setRoute('documents', last.document.id) }, made.length > 1 ? 'Open the last one' : 'Open it'));
    } else result.replaceChildren();
  };
  const run = async (fn, label) => {
    setIngest({ busy: true, label, share: null, made: null, error: null });
    try {
      const made = await fn();
      const last = made[made.length - 1];
      S.docs = null;
      setIngest({ busy: false, made });
      undoable(made.length > 1 ? `Ingested ${plural(made.length, 'document')}.` : `Ingested ${last.document.title}.`, made);
      await refreshAll();
    } catch (e) { setIngest({ busy: false, error: ingestError(e) }); }
  };
  const waitLabel = () => (mode.value === 'llm' ? 'Extracting with the LLM. This can take a minute.' : 'Linking the document to the graph.');
  const files = (list) => {
    const big = list.find((f) => f.size > INGEST_MAX_BYTES);
    if (big) { setIngest({ busy: false, made: null, error: ingestError(new ApiError(413, `${big.name} has ${(big.size / 1048576).toFixed(1)} MB; the limit is 64 MB.`)) }); return; }
    const wait = waitLabel();
    run(async () => {
      const made = [];
      for (const [k, f] of list.entries()) {
        const p = params();
        const qs = new URLSearchParams({ name: f.name, mode: p.mode, tags: p.tags.join(','), link: p.link ? '1' : '0', ...(p.title && list.length === 1 ? { title: p.title } : {}), ...(p.backend ? { backend: p.backend } : {}), ...(p.model ? { model: p.model } : {}) });
        const head = list.length > 1 ? `${k + 1} of ${list.length}: ` : '';
        setIngest({ label: `Uploading ${head}${f.name}`, share: 0 });
        made.push(await uploadFile(`/api/ingest?${qs}`, f, (share) => setIngest(share >= 1 ? { label: `${head}${wait}`, share: null } : { share })));
      }
      return made;
    }, `Uploading ${list.length > 1 ? plural(list.length, 'file') : list[0].name}`);
  };
  pane.replaceChildren(h('div', { class: 'page-inner ingest', style: 'padding:0;max-width:760px' },
    h('div', { class: 'page-head' }, h('div', null, h('h1', { text: 'Ingest' }), h('p', { text: 'Turn a file, a web page or pasted text into a document and connect it to the graph.' })), h('button', { class: 'quiet', onclick: () => { if (!(ingestState && ingestState.busy)) ingestState = null; setRoute('documents'); } }, 'Close')),
    h('div', { class: 'row fields' }, h('label', { class: 'field', for: 'ingest-mode' }, h('span', { text: 'Extraction' }), mode), h('label', { class: 'field', for: 'ingest-title' }, h('span', { text: 'Title' }), title), h('label', { class: 'field', for: 'ingest-tags' }, h('span', { text: 'Tags' }), tags)),
    llmBox,
    h('label', { class: 'row', for: 'ingest-link' }, link, 'Link mentions of known nodes'),
    dropzone(files, 'Markdown, text, code, HTML, PDF, DOCX or images, up to 64 MB each.', INGEST_ACCEPT),
    h('form', { class: 'row', onsubmit: (ev) => { ev.preventDefault(); if (url.value) run(async () => [await api('POST', '/api/ingest', { url: url.value, ...params() })], `Fetching ${url.value}`); } }, h('label', { class: 'field', for: 'ingest-url', style: 'flex:1' }, h('span', { text: 'Web page' }), url), h('button', { type: 'submit' }, 'Fetch')),
    h('form', { class: 'stack', onsubmit: (ev) => { ev.preventDefault(); if (text.value.trim()) run(async () => [await api('POST', '/api/ingest', { text: text.value, name: 'pasted.md', ...params() })], waitLabel()); } }, h('label', { class: 'field', for: 'ingest-text' }, h('span', { text: 'Text' }), text), h('div', { class: 'row' }, h('button', { type: 'submit', class: 'primary' }, 'Ingest text'))),
    progress, result));
  ingestPaint = () => { if (pane.isConnected) paint(); };
  paint();
}

// ----------------------------------------------------------------- history
async function renderHistoryView(root) {
  const page = h('div', { class: 'page' }); const inner = h('div', { class: 'page-inner' }); page.append(inner); root.append(page);
  inner.append(h('div', { class: 'page-head' }, h('div', null, h('h1', { text: 'History' }), h('p', { text: 'Every change from the admin, the API and agents. Undo on a row reverts only that change.' })), h('button', { class: 'primary', onclick: undoLatest }, 'Undo the latest change')));
  const host = h('div'); inner.append(host);
  if (!S.history) { host.append(skeletonState('Loading the history')); try { await loadHistory(); } catch (e) { host.replaceChildren(failedState(e, rerender)); return; } }
  if (!S.history.length) { host.replaceChildren(h('div', { class: 'empty' }, h('p', { text: 'No changes yet. Edits from the map, documents, ingest and agents appear here.' }), h('button', { onclick: () => renderAddNode() }, 'Add a node'))); return; }
  const tb = h('tbody');
  for (const e of S.history) {
    tb.append(h('tr', null, h('td', { class: 'num', text: e.seq }), h('td', { class: 'mono muted', text: when(e.ts) }), h('td', { class: 'mono', text: e.actor }),
      h('td', { text: e.summary }), h('td', { class: 'num', text: num(e.steps) }), h('td', null, e.undone ? h('span', { class: 'badge', text: 'Undone' }) : e.op === 'undo' ? h('span', { class: 'badge', dataset: { sev: 'info' }, text: `Undid ${e.target}` })
        : S.readOnly ? '' : h('button', { class: 'quiet', 'aria-label': `Undo change ${e.seq}`, onclick: () => undoSeqs([e.seq]) }, 'Undo'))));
  }
  host.replaceChildren(h('div', { class: 'table-wrap' }, h('table', { class: 'stack-wide' }, h('thead', null, h('tr', null, h('th', { class: 'num', text: 'Change' }), h('th', { text: 'Time (UTC)' }), h('th', { text: 'By' }), h('th', { text: 'What' }), h('th', { class: 'num', text: 'Steps' }), h('th', { text: 'State' }))), tb)));
}

// ------------------------------------------------------------------ search
function bindSearch() {
  const input = $('#search-input'), list = $('#search-results');
  let results = [], active = -1;
  const close = () => { list.hidden = true; input.setAttribute('aria-expanded', 'false'); active = -1; };
  const choose = (r) => {
    close();
    if (r.kind === 'document') { input.blur(); setRoute('documents', r.id); return; }
    focusNav.title = true; focusNav.back = input;
    setRoute('graph', r.id, S.route.tab === 'graph' ? S.route.q : {});
  };
  // Same label twice: the source file tells the nodes apart.
  const metaOf = (r) => (r.source_file && results.some((o) => o !== r && o.label === r.label) ? `${r.class} · ${shortName(r.source_file, 40)}` : r.class);
  const draw = () => {
    list.replaceChildren(...results.map((r, i) => h('li', { role: 'option', id: `hit-${i}`, 'aria-selected': i === active ? 'true' : 'false', onmousedown: (ev) => { ev.preventDefault(); choose(r); } },
      h('span', { class: 'hit-label' }, breakable(r.label || r.id)), h('span', { class: 'hit-meta', text: r.kind === 'document' ? 'document' : metaOf(r) }),
      r.snippet ? h('span', { class: 'hit-snippet', text: r.snippet }) : null)));
    if (!results.length) list.append(h('li', { class: 'muted', role: 'option', 'aria-disabled': 'true' }, h('span', { class: 'hit-label muted', text: 'Nothing found' })));
    // On phones the list is fixed under the field, as wide as the screen.
    list.style.setProperty('--search-top', `${Math.round(input.getBoundingClientRect().bottom + 4)}px`);
    list.hidden = false; input.setAttribute('aria-expanded', 'true');
    if (active >= 0) input.setAttribute('aria-activedescendant', `hit-${active}`); else input.removeAttribute('aria-activedescendant');
  };
  const run = debounce(async () => {
    const q = input.value.trim();
    if (!q) { close(); return; }
    if (STATIC || !S.view) {
      const lq = q.toLowerCase(); const out = [];
      for (let i = 0; i < graph.n && out.length < 30; i++) if (graph.labels[i].toLowerCase().includes(lq)) out.push({ kind: 'node', id: graph.ids[i], label: graph.labels[i], class: graph.classOf[graph.cls[i]], score: graph.labels[i].toLowerCase() === lq ? 2 : graph.labels[i].toLowerCase().startsWith(lq) ? 1 : 0 });
      out.sort((a, b) => b.score - a.score);
      for (const d of (STATIC ? STATIC.documents : []) || []) if (String(d.title).toLowerCase().includes(lq) || String(d.body || '').toLowerCase().includes(lq)) out.push({ kind: 'document', id: d.id, label: d.title });
      results = out.slice(0, 30);
    } else {
      try { results = (await api('GET', `/api/search?limit=30&q=${enc(q)}`)).results; } catch (e) { results = []; }
      // A document comes back twice: as a document and as its doc: node. Keep one.
      const docIds = new Set(results.filter((r) => r.kind === 'document').map((r) => `doc:${r.id}`));
      results = results.filter((r) => r.kind === 'document' || !docIds.has(r.id));
    }
    active = results.length ? 0 : -1; draw();
  }, 140);
  input.addEventListener('input', run);
  input.addEventListener('focus', () => { if (input.dataset.quiet) { delete input.dataset.quiet; return; } if (input.value.trim()) run(); });
  input.addEventListener('blur', () => setTimeout(close, 120));
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowDown') { ev.preventDefault(); active = Math.min(results.length - 1, active + 1); draw(); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); active = Math.max(0, active - 1); draw(); }
    else if (ev.key === 'Enter') { ev.preventDefault(); if (results[active]) choose(results[active]); }
    else if (ev.key === 'Escape') { close(); input.blur(); }
  });
  document.addEventListener('keydown', (ev) => {
    const tag = (ev.target.tagName || '').toLowerCase();
    if (ev.key === '/' && !['input', 'textarea', 'select'].includes(tag)) { ev.preventDefault(); input.focus(); input.select(); }
    else if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'k') { ev.preventDefault(); input.focus(); input.select(); }
    else if (ev.key === 'Escape' && S.route.tab === 'graph' && !['input', 'textarea', 'select'].includes(tag)) {
      const wb = $('.workbench');
      if (wb && wb.dataset.drawer === 'open') setDrawer(false); else closeNode();
    }
  });
}

// ----------------------------------------------------------------- sign in
let signingIn = false;
function showSignIn() {
  if (signingIn) return; signingIn = true;
  const input = h('input', { type: 'password', id: 'token', autocomplete: 'current-password', required: true });
  const err = h('p', { class: 'error-text', role: 'alert' });
  $('#app').replaceChildren(h('form', { class: 'signin', onsubmit: async (ev) => {
    ev.preventDefault();
    const res = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Graphify-KB': '1' }, body: JSON.stringify({ token: input.value }), credentials: 'same-origin' });
    if (!res.ok) { err.textContent = 'The token is wrong.'; input.setAttribute('aria-invalid', 'true'); return; }
    signingIn = false; boot();
  } }, h('h1', { text: 'Sign in' }), h('p', { class: 'muted', text: 'This knowledge base needs the token that its server prints or reads from GRAPHIFY_KB_TOKEN.' }),
    h('label', { class: 'field', for: 'token' }, h('span', { text: 'Token' }), input), err, h('button', { class: 'primary', type: 'submit' }, 'Sign in')));
  $('#live-state').textContent = 'Signed out';
  input.focus();
}

// -------------------------------------------------------------------- live
let source = null;
function connectLive() {
  if (STATIC) { $('#live-state').textContent = 'Read-only copy'; return; }
  if (source) source.close();
  source = new EventSource('/api/events');
  const state = $('#live-state');
  source.onopen = () => { state.textContent = 'Live'; state.dataset.state = 'live'; syncReadOnly(); };
  source.onerror = () => { state.textContent = 'Reconnecting'; state.dataset.state = 'offline'; };
  // A save on this page refreshes on its own, and its change event comes during that refresh.
  // Wait for a running refresh, then load again only for a newer version.
  const refresh = debounce(async () => {
    try {
      if (refreshing) await refreshing.catch(() => {});
      if (S.view && S.view.version === S.version) return;
      await refreshAll();
    } catch (e) { /* the next event retries */ }
  }, 250);
  source.onmessage = (ev) => {
    let d; try { d = JSON.parse(ev.data); } catch { return; }
    if (d.type === 'hello') { if (S.version && d.version !== S.version) refresh(); S.version = d.version; return; }
    if (d.type === 'change' || d.type === 'reload') {
      S.version = d.version;
      if (d.actor && d.actor !== 'ui') toast(`${d.actor}: ${d.summary || 'graph reloaded'}`);
      if (S.view && S.view.version !== d.version) refresh();
    }
  };
}

// ------------------------------------------------------------------ routing
function rerender() {
  const app = $('#app');
  const r = S.route;
  for (const t of $$('.tab')) { if (t.dataset.tab === r.tab) t.setAttribute('aria-current', 'page'); else t.removeAttribute('aria-current'); }
  // The rebuild drops the focus to the page. Focus in the node inspector comes back to the same field (#8264).
  const insp = $('#inspector'), active = document.activeElement;
  if (r.tab === 'graph' && r.arg && insp && insp.contains(active)) { focusNav.title = true; focusNav.field = (active.dataset && active.dataset.field) || ''; }
  else if (r.tab !== 'graph' || !r.arg) { focusNav.title = false; focusNav.field = ''; }
  app.replaceChildren();
  const views = { graph: renderGraphView, ontology: renderOntologyView, findings: renderFindingsView, documents: renderDocumentsView, history: renderHistoryView };
  const fn = views[r.tab] || renderGraphView;
  if (S.readOnly && r.tab === 'history') { setRoute('graph', '', {}, true); return; }
  Promise.resolve(fn(app)).catch((e) => app.replaceChildren(failedState(e, rerender)));
}
let lastTab = null;
function route() {
  const prev = S.route;
  S.route = parseHash();
  const sameView = lastTab === S.route.tab && S.route.tab === 'graph' && JSON.stringify(prev.q) === JSON.stringify(S.route.q) && $('#inspector');
  lastTab = S.route.tab;
  if (sameView) {
    if (S.route.arg) openNode(S.route.arg, { fromRoute: true }); else { map.select(-1); currentNode = null; renderInspectorEmpty(); }
    return;
  }
  if (S.route.tab === 'graph' && prev.tab === 'graph' && $('#inspector') && prev.arg === S.route.arg) {
    // colour, label or detail mode changed: keep the canvas, redraw the legend
    const left = $('#map-settings'); if (left) { left.replaceChildren(); renderLegend(left); syncCommunity(); map.dirty = true; return; }
  }
  rerender();
}

async function boot() {
  if (DEBUG) Object.assign(DEBUG, { map, graph });
  const app = $('#app');
  app.replaceChildren(h('div', { class: 'view workbench', dataset: { drawer: 'closed', sheet: 'closed' } }, h('aside', { class: 'pane left' }, skeletonState('Loading')), h('section', { class: 'map' }, h('div', { class: 'map-state' }, h('p', { text: 'Loading the graph' }))), h('aside', { class: 'pane right' }, skeletonState('Loading'))));
  try {
    if (STATIC) { $('#kb-name').textContent = STATIC.name || 'Knowledge base'; }
    else {
      await syncReadOnly();
      const stats = await api('GET', '/api/stats');
      S.stats = stats;
      const parts = String(stats.graph || '').split('/');
      // graph.json sits in <project>/graphify-out: show the project folder.
      $('#kb-name').textContent = parts.length >= 3 ? parts[parts.length - 3] : 'Knowledge base';
    }
    // The community overview is small: draw it while the full graph loads.
    if (!STATIC) loadOverview().then(() => { if (!graph.n && S.overview) { graph.loadOverview(S.overview); S.route = parseHash(); lastTab = S.route.tab; if (S.route.tab === 'graph' && !S.view) { rerender(); } } });
    await Promise.all([loadView(), loadOnto()]);
  } catch (e) {
    if (e.status === 401) return;
    app.replaceChildren(failedState(e, boot));
    $('#live-state').textContent = 'Offline'; $('#live-state').dataset.state = 'offline';
    return;
  }
  const firstPaint = graph.metaOnly && S.route.tab === 'graph' && $('#inspector');
  graph.load(S.view);
  updateCounts();
  S.route = parseHash(); lastTab = S.route.tab;
  // Keep the canvas of the first paint: only the panels change.
  if (firstPaint) { const left = $('#map-settings'); left.replaceChildren(); renderLegend(left); if (S.route.arg) openNode(S.route.arg, { fromRoute: true }); else if (map.openComm >= 0) renderCommunity(map.openComm); else syncCommunity(); map.dirty = true; }
  else rerender();
  connectLive();
}

// Tables stack into cards on phones: every cell carries its column name.
function labelCells(root) {
  for (const table of $$('table', root)) {
    if (table.closest('.doc-body')) continue;
    const heads = $$('thead th', table).map((th) => th.textContent.trim());
    if (!heads.length) continue;
    table.classList.add('stackable');
    for (const tr of $$('tbody tr', table)) {
      let col = 0;
      for (const td of tr.children) {
        if (!td.dataset.label && heads[col]) td.dataset.label = heads[col];
        col += td.colSpan || 1;
      }
    }
  }
}
let labelQueued = false;
new MutationObserver(() => {
  if (labelQueued) return; labelQueued = true;
  requestAnimationFrame(() => { labelQueued = false; labelCells($('#app')); });
}).observe($('#app'), { childList: true, subtree: true });

window.addEventListener('hashchange', route);
bindSearch();
boot();
})();
