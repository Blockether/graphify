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
  view: null, onto: null, report: null, docs: null, history: null, stats: null,
  route: { tab: 'graph', arg: '', q: {} },
  version: 0,
};
if (S.readOnly) document.body.dataset.readonly = 'true';

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
  const res = await fetch(path, { method, headers, body: payload, credentials: 'same-origin' });
  if (res.status === 401) { showSignIn(); throw new ApiError(401, 'A token is required.'); }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!res.ok) throw new ApiError(res.status, (data && data.error) || res.statusText);
  return data;
}
const enc = encodeURIComponent;

// ------------------------------------------------------------------ toasts
function toast(message, opts = {}) {
  const box = $('#toasts');
  const el = h('div', { class: 'toast', dataset: { kind: opts.kind || 'info' } }, h('p', { text: message }));
  if (opts.action) {
    el.append(h('button', { class: 'quiet', onclick: async () => { el.remove(); await opts.action.run(); } }, opts.action.label));
  }
  el.append(h('button', { class: 'quiet', 'aria-label': 'Dismiss', onclick: () => el.remove() }, 'Dismiss'));
  box.append(el);
  setTimeout(() => el.remove(), opts.ms || 8000);
}
function fail(err) { toast(err && err.message ? err.message : String(err), { kind: 'error' }); }
async function undoLatest() {
  try { const r = await api('POST', '/api/undo', {}); toast(`Undid: ${r.summary}`); await refreshAll(); } catch (e) { fail(e); }
}
function undoable(message) { toast(message, { action: { label: 'Undo', run: undoLatest } }); }

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
async function loadOnto() { S.onto = STATIC ? STATIC.ontology : await api('GET', '/api/ontology'); }
async function loadReport() { S.report = STATIC ? STATIC.report : await api('GET', '/api/validate?limit=2000'); }
async function loadDocs() { S.docs = STATIC ? STATIC.documents : (await api('GET', '/api/documents')).documents; }
async function loadHistory() { S.history = STATIC ? [] : (await api('GET', '/api/history?limit=200')).history; }
async function refreshAll() {
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
function classColor(name) {
  const spec = S.view && S.view.classes ? S.view.classes[name] : null;
  if (spec) return spec;
  const order = graph.classNames;
  const i = order.indexOf(name);
  const p = palette();
  return p[(i < 0 ? hashStr(name) : i) % (p.length - 1)];
}
function hashStr(s) { let x = 0; for (let i = 0; i < s.length; i++) x = (x * 31 + s.charCodeAt(i)) | 0; return Math.abs(x); }

// ---------------------------------------------------------- layout kernel
// Fruchterman-Reingold forces with a Barnes-Hut quadtree, hub damping and
// gravity. The same function runs in a Web Worker or, as a fallback, here.
function layoutKernel() {
  let n = 0, m = 0, x, y, fx, fy, es, et, mass, pinned, k = 20, temp = 0, iter = 0, maxIter = 500;
  let cap = 0, qx, qy, qs, qm, qmx, qmy, qb, qc, qn = 0, stack;
  const THETA2 = 1.1 * 1.1;
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
  function insert(i) {
    let c = 0, depth = 0;
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
  function build() {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (let i = 0; i < n; i++) { if (x[i] < x0) x0 = x[i]; if (x[i] > x1) x1 = x[i]; if (y[i] < y0) y0 = y[i]; if (y[i] > y1) y1 = y[i]; }
    const s = Math.max(x1 - x0, y1 - y0) + 1;
    qn = 1; grow(4 * n + 8); cell(0, x0, y0, s);
    for (let i = 0; i < n; i++) insert(i);
  }
  function step() {
    if (n === 0) return 0;
    build();
    fx.fill(0); fy.fill(0);
    const k2 = k * k;
    for (let i = 0; i < n; i++) {
      let sp = 0; stack[sp++] = 0;
      let ax = 0, ay = 0;
      const xi = x[i], yi = y[i], mi = mass[i];
      while (sp > 0) {
        const c = stack[--sp];
        const mc = qm[c];
        if (mc === 0) continue;
        if (qc[c] === -1 && qb[c] === i) continue;
        const cx = qmx[c] / mc, cy = qmy[c] / mc;
        let dx = xi - cx, dy = yi - cy;
        let d2 = dx * dx + dy * dy;
        if (qc[c] === -1 || qs[c] * qs[c] < THETA2 * d2) {
          if (d2 < 0.01) { dx = (Math.random() - 0.5); dy = (Math.random() - 0.5); d2 = dx * dx + dy * dy + 0.01; }
          const f = k2 * mi * mc / d2;
          ax += dx * f; ay += dy * f;
        } else {
          const b = qc[c];
          stack[sp++] = b; stack[sp++] = b + 1; stack[sp++] = b + 2; stack[sp++] = b + 3;
        }
      }
      fx[i] += ax; fy[i] += ay;
    }
    for (let e = 0; e < m; e++) {
      const s = es[e], t = et[e];
      const dx = x[t] - x[s], dy = y[t] - y[s];
      const d = Math.sqrt(dx * dx + dy * dy) + 0.01;
      const f = d / k;
      fx[s] += dx * f / mass[s]; fy[s] += dy * f / mass[s];
      fx[t] -= dx * f / mass[t]; fy[t] -= dy * f / mass[t];
    }
    const g = 0.6 / k;
    let moved = 0;
    for (let i = 0; i < n; i++) {
      if (pinned[i]) continue;
      fx[i] -= x[i] * g * mass[i]; fy[i] -= y[i] * g * mass[i];
      const d = Math.sqrt(fx[i] * fx[i] + fy[i] * fy[i]);
      if (d > 0) {
        const lim = Math.min(d, temp * (1 + 2 / Math.sqrt(mass[i])));
        x[i] += fx[i] / d * lim; y[i] += fy[i] / d * lim;
        moved += lim;
      }
    }
    temp *= 0.985; iter++;
    return moved / n;
  }
  return {
    init(d) {
      n = d.n; m = d.m; x = d.x; y = d.y; es = d.es; et = d.et; mass = d.mass; pinned = d.pinned; k = d.k;
      fx = new Float64Array(n); fy = new Float64Array(n); stack = new Int32Array(4 * 64 + 16);
      temp = d.temp || k * 4; iter = 0; maxIter = d.maxIter || 500;
    },
    step,
    done() { return iter >= maxIter || temp < k * 0.01; },
    reheat(t) { temp = Math.max(temp, t || k * 2); iter = Math.max(0, maxIter - 250); },
    pin(i, px, py) { pinned[i] = 1; x[i] = px; y[i] = py; },
    unpin(i) { pinned[i] = 0; },
    positions() { return { x, y }; },
  };
}

function workerMain() {
  const K = layoutKernel();
  let running = false, timer = null;
  function loop() {
    const t0 = Date.now();
    while (!K.done() && Date.now() - t0 < 30) K.step();
    const p = K.positions();
    self.postMessage({ type: 'pos', x: Float32Array.from(p.x), y: Float32Array.from(p.y), done: K.done() });
    if (K.done()) { running = false; return; }
    timer = setTimeout(loop, 0);
  }
  function start() { if (!running) { running = true; timer = setTimeout(loop, 0); } }
  self.onmessage = (ev) => {
    const d = ev.data;
    if (d.type === 'init') { clearTimeout(timer); running = false; K.init(d); start(); }
    else if (d.type === 'pin') { K.pin(d.i, d.x, d.y); K.reheat(d.temp); start(); }
    else if (d.type === 'unpin') { K.unpin(d.i); }
    else if (d.type === 'reheat') { K.reheat(d.temp); start(); }
    else if (d.type === 'stop') { clearTimeout(timer); running = false; }
  };
}

// ------------------------------------------------------------ graph model
const graph = {
  n: 0, m: 0, ids: [], labels: [], cls: null, comm: null, deg: null, kb: null,
  index: new Map(), es: null, et: null, er: null, relations: [], classNames: [],
  x: null, y: null, mass: null, pinned: null, visible: null,
  adjStart: null, adjList: null, order: null, worker: null, kernel: null, running: false,
  load(view) {
    const old = new Map();
    if (this.x) for (let i = 0; i < this.n; i++) old.set(this.ids[i], [this.x[i], this.y[i]]);
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
    this.mass = new Float64Array(n);
    for (let i = 0; i < n; i++) this.mass[i] = 1 + Math.sqrt(this.deg[i]);
    this.pinned = new Uint8Array(n);
    this.visible = new Uint8Array(n).fill(1);
    this.x = new Float64Array(n); this.y = new Float64Array(n);
    const k = 18;
    let fresh = 0;
    const centers = this.communityCenters(k);
    for (let i = 0; i < n; i++) {
      const p = old.get(this.ids[i]);
      if (p) { this.x[i] = p[0]; this.y[i] = p[1]; continue; }
      fresh++;
      const c = centers.get(this.comm[i]) || { x: 0, y: 0, r: k * 4 };
      const a = Math.random() * Math.PI * 2, rr = Math.sqrt(Math.random()) * c.r;
      this.x[i] = c.x + Math.cos(a) * rr; this.y[i] = c.y + Math.sin(a) * rr;
    }
    // A few new nodes join next to their neighbours instead of a random spot.
    if (old.size && fresh < 200) {
      for (let i = 0; i < n; i++) {
        if (old.has(this.ids[i])) continue;
        for (let p = this.adjStart[i]; p < this.adjStart[i + 1]; p++) {
          const e = this.adjList[p]; const o = this.es[e] === i ? this.et[e] : this.es[e];
          if (old.has(this.ids[o])) { this.x[i] = this.x[o] + (Math.random() - 0.5) * k * 2; this.y[i] = this.y[o] + (Math.random() - 0.5) * k * 2; break; }
        }
      }
    }
    this.applyFilter();
    this.startLayout(k, old.size ? (fresh ? k * 1.5 : k * 0.2) : k * 4, old.size ? 220 : 520);
    map.invalidateGrid();
    map.dirty = true;
    if (!old.size) map.fitPending = true;
  },
  communityCenters(k) {
    const size = new Map();
    for (let i = 0; i < this.n; i++) size.set(this.comm[i], (size.get(this.comm[i]) || 0) + 1);
    const list = Array.from(size.entries()).sort((a, b) => b[1] - a[1]);
    const out = new Map();
    let ring = 0;
    list.forEach(([c, s], idx) => {
      const r = Math.sqrt(s) * k * 0.9;
      const a = idx * 2.399963;
      ring += r * 0.6;
      const dist = idx === 0 ? 0 : Math.sqrt(idx) * k * 9 + Math.sqrt(ring) * 2;
      out.set(c, { x: Math.cos(a) * dist, y: Math.sin(a) * dist, r });
    });
    return out;
  },
  startLayout(k, temp, maxIter) {
    const payload = { type: 'init', n: this.n, m: this.m, x: Float64Array.from(this.x), y: Float64Array.from(this.y), es: this.es, et: this.et, mass: this.mass, pinned: this.pinned.slice(), k, temp, maxIter };
    this.running = true; ui.layoutState();
    if (!this.worker && !this.kernel) {
      try {
        const src = `${layoutKernel.toString()}\n(${workerMain.toString()})();`;
        this.worker = new Worker(URL.createObjectURL(new Blob([src], { type: 'text/javascript' })));
        this.worker.onmessage = (ev) => this.onPositions(ev.data);
        this.worker.onerror = () => { this.worker = null; this.kernel = layoutKernel(); this.startLayout(k, temp, maxIter); };
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
    if (d.x.length !== this.n) return;
    this.x.set(d.x); this.y.set(d.y);
    if (d.done) { this.running = false; ui.layoutState(); }
    map.invalidateGrid(); map.dirty = true;
    if (map.followSet) map.fitSet(map.followSet); else if (map.fitPending) map.fit(false);
  },
  stopLayout() { this.running = false; if (this.worker) this.worker.postMessage({ type: 'stop' }); ui.layoutState(); },
  reheat() { this.running = true; ui.layoutState(); if (this.worker) this.worker.postMessage({ type: 'reheat', temp: 30 }); else if (this.kernel) { this.kernel.reheat(30); this.mainLoop(); } },
  pin(i, px, py) {
    this.pinned[i] = 1; this.x[i] = px; this.y[i] = py;
    if (this.worker) this.worker.postMessage({ type: 'pin', i, x: px, y: py, temp: 8 });
    else if (this.kernel) { this.kernel.pin(i, px, py); }
  },
  neighbors(i) {
    const out = [];
    for (let p = this.adjStart[i]; p < this.adjStart[i + 1]; p++) {
      const e = this.adjList[p]; out.push([e, this.es[e] === i ? this.et[e] : this.es[e], this.es[e] === i]);
    }
    return out;
  },
  applyFilter() {
    const hidden = ui.hiddenClasses;
    for (let i = 0; i < this.n; i++) this.visible[i] = hidden.has(this.classOf[this.cls[i]]) ? 0 : 1;
    map.dirty = true; map.invalidateGrid();
  },
};

// ------------------------------------------------------------- map render
const map = {
  canvas: null, ctx: null, w: 0, h: 0, dpr: 1, scale: 1, tx: 0, ty: 0,
  dirty: true, fitPending: false, followSet: null, grid: null, gridAt: 0, cell: 40,
  hover: -1, selected: -1, focusSet: null, colorBy: 'class', labels: 'auto', drag: null,
  colors: {},
  mount(container) {
    this.canvas = h('canvas', { tabindex: '0', role: 'img', 'aria-label': 'Graph map. Use the search box to find a node, then the inspector to read and edit it.' });
    container.append(this.canvas);
    this.ctx = this.canvas.getContext('2d');
    this.colors = {
      edge: cssVar('--color-rule-2'), accent: cssVar('--color-accent'), ink: cssVar('--color-ink'),
      ink2: cssVar('--color-ink-2'), muted: cssVar('--color-muted'), paper: cssVar('--color-paper-3'), cool: cssVar('--color-cool'),
    };
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
  fit(animate = true) {
    const g = graph; if (!g.n) return;
    this.followSet = null;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity, any = false;
    for (let i = 0; i < g.n; i++) { if (!g.visible[i]) continue; any = true; if (g.x[i] < x0) x0 = g.x[i]; if (g.x[i] > x1) x1 = g.x[i]; if (g.y[i] < y0) y0 = g.y[i]; if (g.y[i] > y1) y1 = g.y[i]; }
    if (!any) return;
    const pad = 40, bw = Math.max(x1 - x0, 1), bh = Math.max(y1 - y0, 1);
    const s = Math.min((this.w - pad * 2) / bw, (this.h - pad * 2) / bh, 4);
    this.scale = s; this.tx = this.w / 2 - (x0 + bw / 2) * s; this.ty = this.h / 2 - (y0 + bh / 2) * s;
    this.dirty = true;
    if (!animate) { /* fit follows the layout until the user moves the camera */ }
  },
  centerOn(i, zoom) {
    if (i < 0) return;
    if (zoom) this.scale = Math.max(this.scale, zoom);
    this.tx = this.w / 2 - graph.x[i] * this.scale; this.ty = this.h / 2 - graph.y[i] * this.scale;
    this.fitPending = false; this.followSet = null; this.dirty = true;
  },
  fitSet(set, maxScale = 3) {
    // Frame a node with its neighbours, so a selection shows its context.
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const i of set) { x0 = Math.min(x0, graph.x[i]); y0 = Math.min(y0, graph.y[i]); x1 = Math.max(x1, graph.x[i]); y1 = Math.max(y1, graph.y[i]); }
    if (!isFinite(x0)) return;
    const pad = 60, bw = Math.max(x1 - x0, 40), bh = Math.max(y1 - y0, 40);
    this.scale = Math.min((this.w - pad * 2) / bw, (this.h - pad * 2) / bh, maxScale);
    this.tx = this.w / 2 - (x0 + (x1 - x0) / 2) * this.scale; this.ty = this.h / 2 - (y0 + (y1 - y0) / 2) * this.scale;
    // Keep the frame while the layout still moves, until the user moves the camera.
    this.fitPending = false; this.followSet = set; this.dirty = true;
  },
  invalidateGrid() { this.grid = null; },
  buildGrid() {
    const g = graph, c = this.cell, grid = new Map();
    for (let i = 0; i < g.n; i++) {
      if (!g.visible[i]) continue;
      const key = Math.floor(g.x[i] / c) + ',' + Math.floor(g.y[i] / c);
      let b = grid.get(key); if (!b) grid.set(key, (b = [])); b.push(i);
    }
    this.grid = grid;
  },
  pick(sx, sy) {
    if (!this.grid) this.buildGrid();
    const [wx, wy] = this.toWorld(sx, sy);
    const c = this.cell, gx = Math.floor(wx / c), gy = Math.floor(wy / c);
    const reach = Math.max(1, Math.ceil((10 / this.scale) / c));
    let best = -1, bestD = Infinity;
    for (let ix = gx - reach; ix <= gx + reach; ix++) for (let iy = gy - reach; iy <= gy + reach; iy++) {
      const b = this.grid.get(ix + ',' + iy); if (!b) continue;
      for (const i of b) {
        const dx = graph.x[i] - wx, dy = graph.y[i] - wy, d = Math.sqrt(dx * dx + dy * dy);
        const r = Math.max(this.radius(i), 6 / this.scale);
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
    this.dirty = false;
    const ctx = this.ctx, g = graph, dpr = this.dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);
    if (!g.n) return;
    const s = this.scale, tx = this.tx, ty = this.ty, W = this.w, H = this.h;
    const focus = this.focusSet;
    const inView = (i) => { const sx = g.x[i] * s + tx, sy = g.y[i] * s + ty; return sx > -50 && sx < W + 50 && sy > -50 && sy < H + 50; };
    // edges
    ctx.lineWidth = s > 1.5 ? 1 : 0.6;
    ctx.strokeStyle = this.colors.edge;
    ctx.globalAlpha = focus ? 0.08 : (g.m > 20000 ? 0.22 : 0.35);
    ctx.beginPath();
    for (let e = 0; e < g.m; e++) {
      const a = g.es[e], b = g.et[e];
      if (!g.visible[a] || !g.visible[b]) continue;
      const ax = g.x[a] * s + tx, ay = g.y[a] * s + ty, bx = g.x[b] * s + tx, by = g.y[b] * s + ty;
      if ((ax < 0 && bx < 0) || (ax > W && bx > W) || (ay < 0 && by < 0) || (ay > H && by > H)) continue;
      ctx.moveTo(ax, ay); ctx.lineTo(bx, by);
    }
    ctx.stroke();
    if (this.selected >= 0) {
      ctx.globalAlpha = 0.9; ctx.strokeStyle = this.colors.accent; ctx.lineWidth = 1.4;
      ctx.beginPath();
      for (const [e] of g.neighbors(this.selected)) {
        const a = g.es[e], b = g.et[e];
        ctx.moveTo(g.x[a] * s + tx, g.y[a] * s + ty); ctx.lineTo(g.x[b] * s + tx, g.y[b] * s + ty);
      }
      ctx.stroke();
    }
    // nodes, batched by colour
    const buckets = new Map();
    for (let i = 0; i < g.n; i++) {
      if (!g.visible[i] || !inView(i)) continue;
      const col = this.nodeColor(i);
      let b = buckets.get(col); if (!b) buckets.set(col, (b = [])); b.push(i);
    }
    const rmin = 1.4;
    for (const [col, list] of buckets) {
      ctx.fillStyle = col;
      for (const pass of focus ? [0, 1] : [1]) {
        ctx.globalAlpha = focus ? (pass ? 1 : 0.14) : 0.92;
        ctx.beginPath();
        for (const i of list) {
          if (focus && (pass === 1) !== focus.has(i)) continue;
          const r = Math.max(rmin, Math.min(this.radius(i) * Math.sqrt(s), 28));
          const sx = g.x[i] * s + tx, sy = g.y[i] * s + ty;
          ctx.moveTo(sx + r, sy); ctx.arc(sx, sy, r, 0, Math.PI * 2);
        }
        ctx.fill();
      }
    }
    ctx.globalAlpha = 1;
    // manual knowledge gets a ring in the ink colour
    ctx.strokeStyle = this.colors.ink; ctx.lineWidth = 1; ctx.beginPath();
    for (let i = 0; i < g.n; i++) {
      if (!g.kb[i] || !g.visible[i] || !inView(i)) continue;
      const r = Math.max(rmin, Math.min(this.radius(i) * Math.sqrt(s), 28)) + 2;
      const sx = g.x[i] * s + tx, sy = g.y[i] * s + ty;
      ctx.moveTo(sx + r, sy); ctx.arc(sx, sy, r, 0, Math.PI * 2);
    }
    ctx.globalAlpha = focus ? 0.4 : 0.8; ctx.stroke(); ctx.globalAlpha = 1;
    for (const [i, col, w] of [[this.hover, this.colors.ink, 1.5], [this.selected, this.colors.accent, 2.5]]) {
      if (i < 0 || !g.visible[i]) continue;
      const r = Math.max(rmin, Math.min(this.radius(i) * Math.sqrt(s), 28)) + 3;
      ctx.strokeStyle = col; ctx.lineWidth = w; ctx.beginPath(); ctx.arc(g.x[i] * s + tx, g.y[i] * s + ty, r, 0, Math.PI * 2); ctx.stroke();
    }
    this.drawLabels(inView);
  },
  drawLabels(inView) {
    if (this.labels === 'off') return;
    const ctx = this.ctx, g = graph, s = this.scale, tx = this.tx, ty = this.ty;
    const placed = [];
    const font = `500 12px ${cssVar('--font-body') || 'sans-serif'}`;
    ctx.font = font; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
    const tryLabel = (i, strong) => {
      if (i < 0 || !g.visible[i] || !inView(i)) return;
      const text = g.labels[i].length > 42 ? g.labels[i].slice(0, 41) + '\u2026' : g.labels[i];
      const r = Math.max(1.4, Math.min(this.radius(i) * Math.sqrt(s), 28));
      const sx = g.x[i] * s + tx + r + 4, sy = g.y[i] * s + ty;
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
    let shown = 0;
    const minDeg = s > 2.5 ? 0 : s > 1 ? 1 : s > 0.4 ? 3 : 8;
    for (const i of g.order) {
      if (shown >= budget) break;
      if (g.deg[i] < minDeg) break;
      if (this.focusSet && !this.focusSet.has(i)) continue;
      if (!g.visible[i] || !inView(i)) continue;
      const before = placed.length; tryLabel(i, false); if (placed.length > before) shown++;
    }
  },
  bind() {
    const cv = this.canvas;
    let down = null;
    cv.addEventListener('pointerdown', (ev) => {
      cv.setPointerCapture(ev.pointerId);
      const i = this.pick(ev.offsetX, ev.offsetY);
      down = { x: ev.offsetX, y: ev.offsetY, tx: this.tx, ty: this.ty, node: i, moved: false };
    });
    cv.addEventListener('pointermove', (ev) => {
      if (down) {
        const dx = ev.offsetX - down.x, dy = ev.offsetY - down.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) down.moved = true;
        if (!down.moved) return;
        this.fitPending = false; this.followSet = null;
        if (down.node >= 0 && !S.readOnly) {
          const [wx, wy] = this.toWorld(ev.offsetX, ev.offsetY);
          graph.pin(down.node, wx, wy); this.invalidateGrid();
        } else { this.tx = down.tx + dx; this.ty = down.ty + dy; }
        this.dirty = true;
        return;
      }
      const i = this.pick(ev.offsetX, ev.offsetY);
      if (i !== this.hover) { this.hover = i; this.dirty = true; cv.style.cursor = i >= 0 ? 'pointer' : ''; }
    });
    const up = (ev) => {
      if (!down) return;
      const d = down; down = null;
      if (!d.moved) {
        const i = this.pick(ev.offsetX, ev.offsetY);
        if (i >= 0) openNode(graph.ids[i]); else closeNode();
      }
    };
    cv.addEventListener('pointerup', up);
    cv.addEventListener('pointercancel', () => { down = null; });
    cv.addEventListener('pointerleave', () => { if (this.hover >= 0) { this.hover = -1; this.dirty = true; } });
    cv.addEventListener('dblclick', (ev) => { const i = this.pick(ev.offsetX, ev.offsetY); if (i >= 0) this.centerOn(i, Math.max(this.scale * 2, 1.5)); });
    cv.addEventListener('wheel', (ev) => {
      ev.preventDefault();
      this.zoomAt(ev.offsetX, ev.offsetY, Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0015)));
    }, { passive: false });
    cv.addEventListener('keydown', (ev) => {
      const step = 60;
      if (ev.key === '+' || ev.key === '=') this.zoomAt(this.w / 2, this.h / 2, 1.25);
      else if (ev.key === '-') this.zoomAt(this.w / 2, this.h / 2, 0.8);
      else if (ev.key === 'ArrowLeft') this.tx += step; else if (ev.key === 'ArrowRight') this.tx -= step;
      else if (ev.key === 'ArrowUp') this.ty += step; else if (ev.key === 'ArrowDown') this.ty -= step;
      else if (ev.key === 'f') this.fit(); else return;
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
    const note = $('#map-note');
    if (note) note.textContent = graph.running ? 'Arranging the map' : '';
  },
};
let mapHost = null;
let currentNode = null;

function renderGraphView(root) {
  const left = h('aside', { class: 'pane left', 'aria-label': 'Map settings' });
  const center = h('section', { class: 'map', 'aria-label': 'Graph map' });
  const right = h('aside', { class: 'pane right', id: 'inspector', 'aria-label': 'Inspector' });
  root.append(h('div', { class: 'view workbench' }, left, center, right));
  renderLegend(left);
  if (!S.view) { center.append(skeletonState('Loading the graph')); return; }
  if (!graph.n) {
    center.append(h('div', { class: 'map-state' },
      h('h2', { text: 'The graph is empty' }),
      h('p', { text: 'Nodes from graphify extraction, documents and manual entries appear here.' }),
      S.readOnly ? null : h('button', { class: 'primary', onclick: () => renderAddNode() }, 'Add a node')));
  }
  mapHost = center;
  map.mount(center);
  center.append(h('div', { class: 'map-tools' },
    h('button', { onclick: () => { map.fit(); } }, 'Fit'),
    h('button', { id: 'layout-toggle', 'aria-pressed': 'false', onclick: () => (graph.running ? graph.stopLayout() : graph.reheat()) }, 'Run layout')));
  center.append(h('p', { class: 'map-note', id: 'map-note', 'aria-live': 'polite' }));
  ui.layoutState();
  if (S.route.arg) openNode(S.route.arg, { fromRoute: true }); else renderInspectorEmpty();
}

function renderLegend(left) {
  const mode = S.route.q.color || 'class';
  map.colorBy = mode;
  map.labels = S.route.q.labels || 'auto';
  const seg = (name, value, label, current) => h('button', { 'aria-pressed': current === value ? 'true' : 'false', onclick: () => patchQuery({ [name]: value === 'class' || value === 'auto' ? '' : value }) }, label);
  left.append(h('section', { class: 'stack' },
    h('h2', { text: 'Colour by' }),
    h('div', { class: 'segmented', role: 'group', 'aria-label': 'Colour by' }, seg('color', 'class', 'Class', mode), seg('color', 'community', 'Community', mode)),
    h('h3', { text: 'Labels' }),
    h('div', { class: 'segmented', role: 'group', 'aria-label': 'Labels' }, seg('labels', 'auto', 'Hubs', map.labels), seg('labels', 'more', 'More', map.labels), seg('labels', 'off', 'Off', map.labels))));
  if (!S.view) { left.append(skeletonState('Loading')); return; }
  const list = h('ul', { class: 'legend' });
  if (mode === 'class') {
    for (const name of graph.classNames) {
      const count = graph.classCounts.get(name) || 0;
      const cb = h('input', { type: 'checkbox', checked: !ui.hiddenClasses.has(name), onchange: (ev) => { if (ev.target.checked) ui.hiddenClasses.delete(name); else ui.hiddenClasses.add(name); graph.applyFilter(); } });
      const known = S.onto && S.onto.classes.some((c) => c.name === name);
      list.append(h('li', null, h('label', { title: known ? name : `${name} is not in the ontology` }, cb, h('span', { class: 'swatch', style: `--swatch:${classColor(name)}` }), h('span', { class: 'name' }, name, known ? '' : h('span', { class: 'missing', text: ' (undefined)' })), h('span', { class: 'figure', text: num(count) }))));
    }
    left.append(h('section', null, h('h2', { text: 'Classes' }), h('p', { class: 'hint', text: 'Clear a box to hide that class.' }), list));
  } else {
    const sizes = new Map();
    for (let i = 0; i < graph.n; i++) sizes.set(graph.comm[i], (sizes.get(graph.comm[i]) || 0) + 1);
    const top = Array.from(sizes.entries()).sort((a, b) => b[1] - a[1]).slice(0, 24);
    const p = palette();
    for (const [c, size] of top) {
      const name = c < 0 ? 'No community' : (S.view.communities[String(c)] || `Community ${c}`);
      list.append(h('li', null, h('label', { class: 'static' }, h('span'), h('span', { class: 'swatch', style: `--swatch:${c < 0 ? p[p.length - 1] : p[c % (p.length - 1)]}` }), h('span', { class: 'name', text: name }), h('span', { class: 'figure', text: num(size) }))));
    }
    left.append(h('section', null, h('h2', { text: 'Largest communities' }), list));
  }
  const rels = new Map();
  for (let e = 0; e < graph.m; e++) rels.set(graph.er[e], (rels.get(graph.er[e]) || 0) + 1);
  const rl = h('ul', { class: 'legend' });
  for (const [r, c] of Array.from(rels.entries()).sort((a, b) => b[1] - a[1]).slice(0, 16)) {
    const known = S.onto && S.onto.relations.some((x) => x.name === graph.relations[r]);
    rl.append(h('li', null, h('label', null, h('span'), h('span'), h('span', { class: 'name' }, graph.relations[r], known ? '' : h('span', { class: 'missing', text: ' (undefined)' })), h('span', { class: 'figure', text: num(c) }))));
  }
  left.append(h('section', null, h('h2', { text: 'Relations' }), rl));
}

function skeletonState(label) {
  return h('div', { class: 'skeleton', 'aria-busy': 'true', 'aria-label': label }, h('span'), h('span'), h('span'), h('span'), h('span'));
}
function failedState(err, retry) {
  return h('div', { class: 'failed' }, h('p', { text: err && err.message ? err.message : String(err) }), retry ? h('button', { onclick: retry }, 'Try again') : null);
}

function renderInspectorEmpty() {
  const el = $('#inspector'); if (!el) return;
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
  const label = h('input', { id: 'new-label', required: true, value: prefill.label || '', placeholder: 'For example: Payment service' });
  const cls = h('input', { id: 'new-class', list: 'class-options', value: prefill.cls || 'concept' });
  const err = h('p', { class: 'error-text', role: 'alert' });
  const form = h('form', { class: 'stack', onsubmit: async (ev) => {
    ev.preventDefault();
    if (!label.value.trim()) { label.setAttribute('aria-invalid', 'true'); err.textContent = 'Give the node a label.'; return; }
    try {
      const node = await api('POST', '/api/nodes', { label: label.value.trim(), class: cls.value.trim() || undefined });
      await refreshAll();
      undoable(`Added ${node.label}.`);
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

function closeNode() {
  currentNode = null; map.select(-1);
  if (S.route.tab === 'graph' && S.route.arg) setRoute('graph', '', S.route.q, true);
  renderInspectorEmpty();
}

async function openNode(id, opts = {}) {
  if (S.route.tab !== 'graph') { setRoute('graph', id, S.route.q); return; }
  if (!opts.fromRoute && S.route.arg !== id) { setRoute('graph', id, S.route.q, true); return; }
  currentNode = id;
  const i = graph.index.has(id) ? graph.index.get(id) : -1;
  map.select(i, { center: opts.fromRoute && i >= 0 && !opts.keepCamera, zoom: 1.2 });
  const el = $('#inspector'); if (!el) return;
  el.replaceChildren(skeletonState('Loading the node'));
  let detail;
  try { detail = await nodeDetail(id); } catch (e) { el.replaceChildren(failedState(e, () => openNode(id, opts))); return; }
  if (currentNode !== id) return;
  renderInspector(el, detail);
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
  const shown = h('button', { class: 'editable' + (opts.mono ? ' mono' : ''), type: 'button', title: 'Edit', 'data-write': opts.alwaysShow ? null : true }, value === '' || value === undefined ? (opts.placeholder || 'Empty') : value);
  if (S.readOnly) return h('span', { class: opts.mono ? 'mono' : '' }, value || '');
  shown.addEventListener('click', () => {
    const input = h('input', { value: value || '', 'aria-label': opts.label || 'Value' });
    if (opts.list) input.setAttribute('list', opts.list);
    const save = async () => {
      if (input.value === (value || '')) { input.replaceWith(shown); return; }
      try { await onSave(input.value); } catch (e) { fail(e); input.replaceWith(shown); }
    };
    input.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); save(); } else if (ev.key === 'Escape') input.replaceWith(shown); });
    input.addEventListener('blur', save);
    shown.replaceWith(input); input.focus(); input.select();
  });
  return shown;
}

function renderInspector(el, d) {
  const node = d.node, id = node.id;
  const patchNode = async (attrs, message) => {
    await api('PATCH', `/api/nodes/${enc(id)}`, { attrs });
    await refreshAll();
    undoable(message || `Updated ${node.label}.`);
  };
  const title = h('div', { class: 'inspector-title' },
    h('h2', null, editableValue(node.label, (v) => patchNode({ label: v }, `Renamed to ${v}.`), { label: 'Label', alwaysShow: true })),
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
    if (RESERVED.has(k) || specNames.has(k)) continue;
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
      for (const r of list.slice(0, 60)) {
        ul.append(h('li', null, h('span', { class: 'swatch', style: `--swatch:${classColor(r.other_class || '')}` }),
          h('button', { class: 'link', onclick: () => setRoute('graph', r.other, S.route.q) }, r.other_label),
          r.id && !S.readOnly ? h('button', { class: 'quiet', 'aria-label': `Remove the ${rel} connection to ${r.other_label}`, onclick: async () => {
            try { await api('DELETE', `/api/edges/${enc(r.id)}`); await refreshAll(); undoable(`Removed ${rel} to ${r.other_label}.`); openNode(id, { fromRoute: true, keepCamera: true }); } catch (e) { fail(e); }
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
    try { await api('POST', '/api/edges', { source: s, target: t, relation: rel.value.trim() }); await refreshAll(); undoable(`Linked ${rel.value.trim()}.`); openNode(id, { fromRoute: true, keepCamera: true }); } catch (e) { fail(e); }
  } }, h('h3', { text: 'Add a connection' }), h('div', { class: 'row' }, rel, dir), target, targets, datalist('relation-options', ontologyRelations()), h('button', { type: 'submit' }, 'Link'));
  const docs = (d.in || []).filter((r) => r.relation === 'mentions' && String(r.other).startsWith('doc:'));
  const docSection = h('section', null, h('h3', { text: 'Documents' }),
    docs.length ? h('ul', { class: 'conn-list' }, docs.map((r) => h('li', null, h('span'), h('button', { class: 'link', onclick: () => setRoute('documents', r.other.slice(4)) }, r.other_label), h('span')))) : h('p', { class: 'muted', text: 'No document mentions this node.' }),
    node.doc_id ? h('button', { onclick: () => setRoute('documents', node.doc_id) }, 'Open this document') : null,
    S.readOnly ? null : h('button', { onclick: () => newDocument({ title: `Notes on ${node.label}`, nodes: [id] }) }, 'Write a document about this node'));
  const danger = h('section', { 'data-write': true }, h('button', { class: 'danger', onclick: async () => {
    try { const r = await api('DELETE', `/api/nodes/${enc(id)}`); closeNode(); await refreshAll(); undoable(`Deleted ${node.label}${r.edges_deleted ? ` and ${plural(r.edges_deleted, 'connection')}` : ''}.`); } catch (e) { fail(e); }
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
    try { const data = { ...o, strict: ev.target.checked }; delete data.inferred; delete data.problems; S.onto = await api('PUT', '/api/ontology', data); S.report = null; undoable(ev.target.checked ? 'Strict mode is on.' : 'Strict mode is off.'); rerender(); } catch (e) { fail(e); }
  } });
  inner.append(h('div', { class: 'page-head' },
    h('div', null, h('h1', { text: 'Ontology' }), h('p', { text: o.inferred ? 'Inferred from the extracted graph. The first change saves it to ontology.json.' : 'Saved in ontology.json next to graph.json.' })),
    h('div', { class: 'row' },
      h('label', { class: 'row', for: 'strict' }, strict, 'Unknown classes and relations are errors'),
      h('button', { 'data-write': true, onclick: async () => { try { S.onto = await api('POST', '/api/ontology/infer', { save: true }); S.report = null; undoable('Inferred the ontology from the graph.'); rerender(); } catch (e) { fail(e); } } }, 'Infer from graph'),
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
  try { S.onto = await api('PUT', `/api/ontology/classes/${enc(spec.name)}`, spec); S.report = null; undoable(message || `Saved class ${spec.name}.`); patchQuery({ class: spec.name, new: '' }); rerender(); } catch (e) { fail(e); }
}
async function saveRelation(spec, message) {
  try { S.onto = await api('PUT', `/api/ontology/relations/${enc(spec.name)}`, spec); S.report = null; undoable(message || `Saved relation ${spec.name}.`); patchQuery({ relation: spec.name, new: '' }); rerender(); } catch (e) { fail(e); }
}

function classTable(o, cu, sel) {
  const body = h('tbody');
  const rows = o.classes.slice().sort((a, b) => (cu.get(b.name) || 0) - (cu.get(a.name) || 0));
  if (sel.new === 'class') body.append(h('tr', null, h('td', { class: 'editor-cell', colspan: '4' }, classEditor({ name: '', parent: null, description: '', properties: [] }, o, true))));
  for (const c of rows) {
    const open = sel.class === c.name;
    body.append(h('tr', { class: 'clickable', 'aria-selected': open ? 'true' : 'false', tabindex: '0', onclick: () => patchQuery({ class: open ? '' : c.name, new: '' }), onkeydown: (ev) => { if (ev.key === 'Enter') patchQuery({ class: open ? '' : c.name }); } },
      h('td', null, h('span', { class: 'row' }, h('span', { class: 'swatch', style: `--swatch:${classColor(c.name)}` }), c.name)),
      h('td', { text: c.parent || '' }),
      h('td', { class: 'num', text: num(cu.get(c.name) || 0) }),
      h('td', { class: 'num', text: num((c.properties || []).length) })));
    if (open) body.append(h('tr', null, h('td', { class: 'editor-cell', colspan: '4' }, classEditor(c, o, false))));
  }
  return h('section', { class: 'stack' }, h('h2', { text: 'Classes' }),
    rows.length ? h('div', { class: 'table-wrap' }, h('table', null, h('thead', null, h('tr', null, h('th', { text: 'Name' }), h('th', { text: 'Parent' }), h('th', { class: 'num', text: 'Nodes' }), h('th', { class: 'num', text: 'Properties' }))), body))
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
        : h('button', { type: 'button', class: 'danger', onclick: async () => { try { S.onto = await api('DELETE', `/api/ontology/classes/${enc(c.name)}`); S.report = null; undoable(`Removed class ${c.name}.`); patchQuery({ class: '' }); rerender(); } catch (e) { fail(e); } } }, 'Remove class')));
}

function relationTable(o, ru, sel) {
  const body = h('tbody');
  const rows = o.relations.slice().sort((a, b) => (ru.get(b.name) || 0) - (ru.get(a.name) || 0));
  if (sel.new === 'relation') body.append(h('tr', null, h('td', { class: 'editor-cell', colspan: '4' }, relationEditor({ name: '', domain: [], range: [], cardinality: 'many', description: '' }, o, true))));
  for (const r of rows) {
    const open = sel.relation === r.name;
    body.append(h('tr', { class: 'clickable', 'aria-selected': open ? 'true' : 'false', tabindex: '0', onclick: () => patchQuery({ relation: open ? '' : r.name, new: '' }), onkeydown: (ev) => { if (ev.key === 'Enter') patchQuery({ relation: open ? '' : r.name }); } },
      h('td', { text: r.name }),
      h('td', { class: 'muted', text: (r.domain && r.domain.length ? r.domain.join(', ') : 'Any class') + ' to ' + (r.range && r.range.length ? r.range.join(', ') : 'any class') }),
      h('td', { text: r.cardinality === 'one' ? 'At most one' : 'Many' }),
      h('td', { class: 'num', text: num(ru.get(r.name) || 0) })));
    if (open) body.append(h('tr', null, h('td', { class: 'editor-cell', colspan: '4' }, relationEditor(r, o, false))));
  }
  return h('section', { class: 'stack' }, h('h2', { text: 'Relations' }),
    rows.length ? h('div', { class: 'table-wrap' }, h('table', null, h('thead', null, h('tr', null, h('th', { text: 'Name' }), h('th', { text: 'From and to' }), h('th', { text: 'Per node' }), h('th', { class: 'num', text: 'Edges' }))), body))
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
        : h('button', { type: 'button', class: 'danger', onclick: async () => { try { S.onto = await api('DELETE', `/api/ontology/relations/${enc(r.name)}`); S.report = null; undoable(`Removed relation ${r.name}.`); patchQuery({ relation: '' }); rerender(); } catch (e) { fail(e); } } }, 'Remove relation')));
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
    const W = r.width, H = r.height, cx = W / 2, cy = H / 2 + 10, R = Math.min(W, H) / 2 - 76;
    const pos = new Map();
    names.forEach((n, i) => { const a = (i / names.length) * Math.PI * 2 - Math.PI / 2; pos.set(n, names.length === 1 ? [cx, cy] : [cx + Math.cos(a) * R * (W > H ? 1.6 : 1), cy + Math.sin(a) * R]); });
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
    // Move a label down until it clears the labels drawn before it.
    const placed = [];
    for (const [t, x, y0] of labels) {
      ctx.font = `12px ${cssVar('--font-body')}`; const w = ctx.measureText(t).width + 10;
      let y = y0;
      for (let k = 0; k < 6 && placed.some((b) => Math.abs(b.x - x) < (b.w + w) / 2 && Math.abs(b.y - y) < 19); k++) y += 20;
      placed.push({ x, y, w }); label(t, x, y);
    }
    const maxN = Math.max(1, ...names.map((n) => cu.get(n) || 0));
    for (const n of names) {
      const [x, y] = pos.get(n); const rad = 6 + 14 * Math.sqrt((cu.get(n) || 0) / maxN);
      ctx.fillStyle = classColor(n); ctx.beginPath(); ctx.arc(x, y, rad, 0, Math.PI * 2); ctx.fill();
      const spec = o.classes.find((c) => c.name === n);
      if (spec && spec.parent && pos.get(spec.parent)) { const [px, py] = pos.get(spec.parent); ctx.setLineDash([3, 4]); ctx.strokeStyle = cssVar('--color-muted'); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(px, py); ctx.stroke(); ctx.setLineDash([]); }
      ctx.fillStyle = cssVar('--color-ink'); ctx.font = `500 13px ${cssVar('--font-body')}`; ctx.textAlign = 'center'; ctx.fillText(n, x, y + rad + 16);
      ctx.fillStyle = cssVar('--color-muted'); ctx.font = `12px ${cssVar('--font-outlier')}`; ctx.fillText(num(cu.get(n) || 0), x, y + rad + 31);
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
  const codes = h('div', { class: 'row' }, Object.entries(r.by_code).map(([c, n]) => h('button', { class: 'chip', 'aria-pressed': code === c ? 'true' : 'false', onclick: () => patchQuery({ code: code === c ? '' : c, page: '' }) }, c.replace(/_/g, ' '), ' ', h('span', { class: 'figure', text: num(n) }))));
  const rows = r.findings.filter((f) => (!sev || f.severity === sev) && (!code || f.code === code));
  const pageNo = Math.max(0, parseInt(S.route.q.page || '0', 10));
  const per = 100;
  const tbody = h('tbody');
  for (const f of rows.slice(pageNo * per, pageNo * per + per)) {
    tbody.append(h('tr', { class: f.node ? 'clickable' : '', tabindex: f.node ? '0' : null, onclick: () => f.node && setRoute('graph', f.node), onkeydown: (ev) => { if (ev.key === 'Enter' && f.node) setRoute('graph', f.node); } },
      h('td', null, h('span', { class: 'badge', dataset: { sev: f.severity }, text: f.severity === 'info' ? 'note' : f.severity })),
      h('td', { class: 'mono', text: f.code }),
      h('td', { text: f.message }),
      h('td', { class: 'mono muted', text: f.node || (f.ontology_class || f.ontology_relation || '') })));
  }
  body.append(h('div', { class: 'row spread' }, tabs, h('p', { class: 'muted', text: r.truncated ? `Showing the first ${num(r.findings.length)} of ${num(r.total)} findings.` : `${plural(rows.length, 'finding')}` })), codes,
    rows.length ? h('div', { class: 'table-wrap' }, h('table', null, h('thead', null, h('tr', null, h('th', { text: 'Severity' }), h('th', { text: 'Check' }), h('th', { text: 'Finding' }), h('th', { text: 'Subject' }))), tbody))
      : h('div', { class: 'empty' }, h('p', { text: 'Nothing matches this filter.' }), h('button', { onclick: () => patchQuery({ severity: '', code: '' }) }, 'Clear the filter')),
    rows.length > per ? h('div', { class: 'row' }, h('button', { disabled: pageNo === 0, onclick: () => patchQuery({ page: String(pageNo - 1) }) }, 'Previous'), h('span', { class: 'muted figure', text: `${pageNo + 1} / ${Math.ceil(rows.length / per)}` }), h('button', { disabled: (pageNo + 1) * per >= rows.length, onclick: () => patchQuery({ page: String(pageNo + 1) }) }, 'Next')) : null);
}

// ---------------------------------------------------------------- markdown
function escapeHtml(s) { return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function assetUrl(docId, name) {
  return STATIC ? `kb/documents/${enc(docId)}/assets/${enc(name)}` : `/api/documents/${enc(docId)}/assets/${enc(name)}`;
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
  root.append(h('div', { class: 'view docs' }, left, right));
  left.append(h('div', { class: 'row spread' }, h('h1', { style: 'font-size:var(--text-lg)', text: 'Documents' }),
    h('div', { class: 'row', 'data-write': true }, h('button', { onclick: () => setRoute('documents', '', { ingest: '1' }) }, 'Ingest'), h('button', { class: 'primary', onclick: () => newDocument({}) }, 'New document'))));
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
        h('td', null, h('div', { text: d.title }), d.tags && d.tags.length ? h('div', { class: 'hint', text: d.tags.join(', ') }) : null),
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
  try {
    const doc = await api('POST', '/api/documents', { title: title || 'Untitled document', body: '', nodes: nodes || [] });
    S.docs = null; await refreshAll();
    undoable(`Created ${doc.title}.`);
    setRoute('documents', doc.id, { edit: '1' });
  } catch (e) { fail(e); }
}

async function renderDocument(pane, id) {
  pane.replaceChildren(skeletonState('Loading the document'));
  let doc;
  try {
    doc = STATIC ? STATIC.documents.find((d) => d.id === id) : await api('GET', `/api/documents/${enc(id)}`);
    if (!doc) throw new Error(`Document ${id} does not exist.`);
  } catch (e) { pane.replaceChildren(failedState(e, () => renderDocument(pane, id))); return; }
  const editing = S.route.q.edit === '1' && !S.readOnly;
  const save = async (fields, message) => {
    try { await api('PATCH', `/api/documents/${enc(id)}`, fields); S.docs = null; await refreshAll(); undoable(message || 'Saved the document.'); } catch (e) { fail(e); }
  };
  const head = h('div', { class: 'page-head' },
    h('div', { class: 'stack' }, h('h1', { class: 'title-edit' }, editableValue(doc.title, (v) => save({ title: v }, `Renamed to ${v}.`), { label: 'Title', alwaysShow: true })),
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
        h('td', null, h('a', { class: 'link', href: assetUrl(id, a.name), target: '_blank', rel: 'noopener' }, a.name)),
        h('td', { class: 'muted', text: a.mime }),
        h('td', { class: 'num', text: bytes(a.size) }),
        h('td', { 'data-write': true }, h('button', { class: 'quiet', onclick: async () => { try { await api('DELETE', `/api/documents/${enc(id)}/assets/${enc(a.name)}`); S.docs = null; undoable(`Removed ${a.name}.`); renderDocument(pane, id); } catch (e) { fail(e); } } }, 'Remove'))));
    }
    assets.append(h('div', { class: 'table-wrap' }, h('table', null, tb)));
    const images = doc.assets.filter((a) => (a.mime || '').startsWith('image/'));
    if (images.length && !images.some((a) => (doc.body || '').includes(a.name))) assets.append(h('div', { class: 'row' }, images.slice(0, 6).map((a) => h('img', { src: assetUrl(id, a.name), alt: a.name, style: 'max-height:140px;width:auto;border-radius:var(--radius-pill)' }))));
  } else assets.append(h('p', { class: 'muted', text: 'No files. Attach images, PDFs or any other file.' }));
  if (!S.readOnly) assets.append(dropzone(async (files) => {
    for (const f of files) {
      try { await api('POST', `/api/documents/${enc(id)}/assets?name=${enc(f.name)}`, f, { headers: { 'Content-Type': f.type || 'application/octet-stream' } }); } catch (e) { fail(e); return; }
    }
    S.docs = null; undoable(`Attached ${plural(files.length, 'file')}.`); renderDocument(pane, id);
  }, 'Attach files'));
  const danger = h('section', { 'data-write': true }, h('button', { class: 'danger', onclick: async () => {
    try { await api('DELETE', `/api/documents/${enc(id)}`); S.docs = null; await refreshAll(); undoable(`Deleted ${doc.title}.`); setRoute('documents'); } catch (e) { fail(e); }
  } }, 'Delete document'));
  pane.replaceChildren(h('div', { class: 'page-inner', style: 'padding:0' }, head, body, linked, assets, danger));
}

function dropzone(onFiles, label) {
  const input = h('input', { type: 'file', multiple: true, 'aria-label': label });
  const zone = h('div', { class: 'dropzone' }, h('label', { class: 'field' }, h('span', { text: label }), input), h('p', { class: 'hint', text: 'Or drop files here.' }));
  input.addEventListener('change', () => { if (input.files.length) onFiles(Array.from(input.files)); });
  zone.addEventListener('dragover', (ev) => { ev.preventDefault(); zone.dataset.over = 'true'; });
  zone.addEventListener('dragleave', () => { zone.dataset.over = 'false'; });
  zone.addEventListener('drop', (ev) => { ev.preventDefault(); zone.dataset.over = 'false'; if (ev.dataTransfer.files.length) onFiles(Array.from(ev.dataTransfer.files)); });
  return zone;
}

function renderIngest(pane) {
  const mode = h('select', { id: 'ingest-mode', 'aria-label': 'Extraction' }, h('option', { value: 'text', text: 'Match node labels (no LLM)' }), h('option', { value: 'llm', text: 'Extract nodes and edges with an LLM' }));
  const title = h('input', { id: 'ingest-title', placeholder: 'From the first heading or the file name' });
  const tags = h('input', { id: 'ingest-tags', placeholder: 'Comma separated' });
  const url = h('input', { id: 'ingest-url', type: 'url', placeholder: 'https://example.com/page' });
  const text = h('textarea', { id: 'ingest-text', class: 'code', style: 'min-height:160px', placeholder: 'Paste markdown or plain text' });
  const result = h('div', { class: 'stack', 'aria-live': 'polite' });
  const params = () => ({ mode: mode.value, title: title.value.trim() || undefined, tags: tags.value.split(',').map((t) => t.trim()).filter(Boolean) });
  const done = async (r) => {
    S.docs = null; await refreshAll();
    result.replaceChildren(h('p', null, `Created ${r.document.title}. `, r.linked ? `Linked ${plural(r.linked, 'node')} by name. ` : '', r.nodes_added.length ? `The LLM added ${plural(r.nodes_added.length, 'node')} and ${plural(r.edges_added, 'edge')}. ` : ''), ...(r.notes || []).map((n) => h('p', { class: 'hint', text: n })), h('button', { onclick: () => setRoute('documents', r.document.id) }, 'Open it'));
    undoable(`Ingested ${r.document.title}.`);
  };
  const busy = (on) => { $$('button, input, select, textarea', pane).forEach((x) => { x.disabled = on; }); if (on) result.replaceChildren(h('p', { class: 'muted', text: mode.value === 'llm' ? 'Extracting with the LLM. This can take a minute.' : 'Ingesting.' })); };
  const run = async (fn) => { busy(true); try { await done(await fn()); } catch (e) { result.replaceChildren(h('p', { class: 'error-text', text: e.message })); } finally { busy(false); } };
  pane.replaceChildren(h('div', { class: 'page-inner', style: 'padding:0;max-width:760px' },
    h('div', { class: 'page-head' }, h('div', null, h('h1', { text: 'Ingest' }), h('p', { text: 'Turn a file, a web page or pasted text into a document and connect it to the graph.' })), h('button', { class: 'quiet', onclick: () => setRoute('documents') }, 'Close')),
    h('div', { class: 'row' }, h('label', { class: 'field', for: 'ingest-mode' }, h('span', { text: 'Extraction' }), mode), h('label', { class: 'field', for: 'ingest-title' }, h('span', { text: 'Title' }), title), h('label', { class: 'field', for: 'ingest-tags' }, h('span', { text: 'Tags' }), tags)),
    h('p', { class: 'hint', text: 'LLM extraction uses the backend that graphify finds in your environment, such as ANTHROPIC_API_KEY or GEMINI_API_KEY, and costs tokens.' }),
    dropzone((files) => run(async () => { let last; for (const f of files) { const p = params(); const qs = new URLSearchParams({ name: f.name, mode: p.mode, ...(p.title ? { title: p.title } : {}), tags: p.tags.join(',') }); last = await api('POST', `/api/ingest?${qs}`, f, { headers: { 'Content-Type': f.type || 'application/octet-stream' } }); } return last; }), 'Files: markdown, text, HTML, PDF, images'),
    h('form', { class: 'row', onsubmit: (ev) => { ev.preventDefault(); if (url.value) run(() => api('POST', '/api/ingest', { url: url.value, ...params() })); } }, h('label', { class: 'field', for: 'ingest-url', style: 'flex:1' }, h('span', { text: 'Web page' }), url), h('button', { type: 'submit' }, 'Fetch')),
    h('form', { class: 'stack', onsubmit: (ev) => { ev.preventDefault(); if (text.value.trim()) run(() => api('POST', '/api/ingest', { text: text.value, name: 'pasted.md', ...params() })); } }, h('label', { class: 'field', for: 'ingest-text' }, h('span', { text: 'Text' }), text), h('div', { class: 'row' }, h('button', { type: 'submit', class: 'primary' }, 'Ingest text'))),
    result));
}

// ----------------------------------------------------------------- history
async function renderHistoryView(root) {
  const page = h('div', { class: 'page' }); const inner = h('div', { class: 'page-inner' }); page.append(inner); root.append(page);
  inner.append(h('div', { class: 'page-head' }, h('div', null, h('h1', { text: 'History' }), h('p', { text: 'Every change from the admin, the API and agents. Undo walks back one change at a time.' })), h('button', { class: 'primary', onclick: undoLatest }, 'Undo the latest change')));
  const host = h('div'); inner.append(host);
  if (!S.history) { host.append(skeletonState('Loading the history')); try { await loadHistory(); } catch (e) { host.replaceChildren(failedState(e, rerender)); return; } }
  if (!S.history.length) { host.replaceChildren(h('div', { class: 'empty' }, h('p', { text: 'No changes yet. Edits from the map, documents, ingest and agents appear here.' }), h('button', { onclick: () => renderAddNode() }, 'Add a node'))); return; }
  const tb = h('tbody');
  for (const e of S.history) {
    tb.append(h('tr', null, h('td', { class: 'num', text: e.seq }), h('td', { class: 'mono muted', text: when(e.ts) }), h('td', { class: 'mono', text: e.actor }),
      h('td', { text: e.summary }), h('td', { class: 'num', text: num(e.steps) }), h('td', null, e.undone ? h('span', { class: 'badge', text: 'Undone' }) : e.op === 'undo' ? h('span', { class: 'badge', dataset: { sev: 'info' }, text: `Undid ${e.target}` }) : '')));
  }
  host.replaceChildren(h('div', { class: 'table-wrap' }, h('table', null, h('thead', null, h('tr', null, h('th', { class: 'num', text: 'Change' }), h('th', { text: 'Time (UTC)' }), h('th', { text: 'By' }), h('th', { text: 'What' }), h('th', { class: 'num', text: 'Steps' }), h('th', { text: 'State' }))), tb)));
}

// ------------------------------------------------------------------ search
function bindSearch() {
  const input = $('#search-input'), list = $('#search-results');
  let results = [], active = -1;
  const close = () => { list.hidden = true; input.setAttribute('aria-expanded', 'false'); active = -1; };
  const choose = (r) => {
    close(); input.blur();
    if (r.kind === 'document') setRoute('documents', r.id);
    else setRoute('graph', r.id, S.route.tab === 'graph' ? S.route.q : {});
  };
  const draw = () => {
    list.replaceChildren(...results.map((r, i) => h('li', { role: 'option', id: `hit-${i}`, 'aria-selected': i === active ? 'true' : 'false', onmousedown: (ev) => { ev.preventDefault(); choose(r); } },
      h('span', { class: 'hit-label', text: r.label }), h('span', { class: 'hit-meta', text: r.kind === 'document' ? 'document' : r.class }),
      r.snippet ? h('span', { class: 'hit-snippet', text: r.snippet }) : null)));
    if (!results.length) list.append(h('li', { class: 'muted', role: 'option', 'aria-disabled': 'true' }, h('span', { class: 'hit-label muted', text: 'Nothing found' })));
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
    }
    active = results.length ? 0 : -1; draw();
  }, 140);
  input.addEventListener('input', run);
  input.addEventListener('focus', () => { if (input.value.trim()) run(); });
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
    else if (ev.key === 'Escape' && S.route.tab === 'graph' && !['input', 'textarea', 'select'].includes(tag)) closeNode();
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
  source.onopen = () => { state.textContent = 'Live'; state.dataset.state = 'live'; };
  source.onerror = () => { state.textContent = 'Reconnecting'; state.dataset.state = 'offline'; };
  const refresh = debounce(async () => {
    try { await refreshAll(); } catch (e) { /* the next event retries */ }
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
    // colour or label mode changed: keep the canvas, redraw the legend
    const left = $('.pane.left'); if (left) { left.replaceChildren(); renderLegend(left); map.dirty = true; return; }
  }
  rerender();
}

async function boot() {
  const app = $('#app');
  app.replaceChildren(h('div', { class: 'view workbench' }, h('aside', { class: 'pane left' }, skeletonState('Loading')), h('section', { class: 'map' }, h('div', { class: 'map-state' }, h('p', { text: 'Loading the graph' }))), h('aside', { class: 'pane right' }, skeletonState('Loading'))));
  try {
    if (STATIC) { $('#kb-name').textContent = STATIC.name || 'Knowledge base'; }
    else {
      const stats = await api('GET', '/api/stats');
      S.stats = stats;
      const parts = String(stats.graph || '').split('/');
      // graph.json sits in <project>/graphify-out: show the project folder.
      $('#kb-name').textContent = parts.length >= 3 ? parts[parts.length - 3] : 'Knowledge base';
    }
    await Promise.all([loadView(), loadOnto()]);
  } catch (e) {
    if (e.status === 401) return;
    app.replaceChildren(failedState(e, boot));
    $('#live-state').textContent = 'Offline'; $('#live-state').dataset.state = 'offline';
    return;
  }
  graph.load(S.view);
  updateCounts();
  S.route = parseHash(); lastTab = S.route.tab;
  rerender();
  connectLive();
}

window.addEventListener('hashchange', route);
bindSearch();
boot();
})();
