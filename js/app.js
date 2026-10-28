import { loadMarket, loadHistory, pruneHistory, latestUpdateStamp, pool } from './api.js';
import { historyStats, suggest, geTax, netSell, breakEvenSell, parseGp } from './analysis.js';
import { loadSettings, saveSettings, loadFlips, saveFlips } from './store.js';

const POLL_MS = 10 * 60 * 1000;
const $ = (s, root = document) => root.querySelector(s);

// ---------- tiny DOM helper (text only, never innerHTML with data) ----------
function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  return el;
}

const fmt = new Intl.NumberFormat('en-US');
const fmtC = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });
const gp = (n) => fmt.format(Math.round(n));
const gpC = (n) => fmtC.format(Math.round(n));
const pct = (x, d = 1) => `${(x * 100).toFixed(d)}%`;
const signed = (n) => (n > 0 ? '+' : '') + gp(n);
const wikiUrl = (name) => `https://runescape.wiki/w/${encodeURIComponent(name.replaceAll(' ', '_'))}`;
const geUrl = (id) => `https://secure.runescape.com/m=itemdb_rs/viewitem?obj=${id}`;
const iconUrl = (name) => `https://runescape.wiki/images/${encodeURIComponent(name.replaceAll(' ', '_'))}.png`;

// ---------- state ----------
const state = {
  settings: loadSettings(),
  flips: loadFlips(),
  market: null,              // {items, updated}
  byId: new Map(),
  stats: new Map(),          // id -> historyStats
  suggestions: [],
  nextPoll: 0,
  runToken: 0,
};
const cfg = () => {
  const s = state.settings;
  return { budget: s.budget, taxRate: s.taxPct / 100, capture: s.capturePct / 100, bandK: s.bandK, dipZ: s.dipZ };
};

// ---------- status / progress / errors ----------
function setStatus(text) { $('#status').textContent = text; }
function setProgress(done, total) {
  const bar = $('#progress');
  bar.classList.toggle('hidden', !total || done >= total);
  const v = total ? Math.round((done / total) * 100) : 0;
  bar.setAttribute('aria-valuenow', v);
  bar.firstElementChild.style.width = `${v}%`;
}
function showError(msg) {
  $('#error').classList.toggle('hidden', !msg);
  $('#error-text').textContent = msg || '';
}

// ---------- data pipeline ----------
function pickCandidates() {
  const s = state.settings, c = cfg();
  return state.market.items
    .filter((it) => it.price >= s.minPrice && it.volume >= s.minVolume && (s.members || !it.members) && it.name !== 'Bond')
    .map((it) => {
      const qty = Math.min(it.limit > 0 ? it.limit : Infinity, (it.volume * c.capture) / 6, c.budget / it.price);
      return { it, turnover: qty >= 1 ? qty * it.price : 0 };
    })
    .filter((x) => x.turnover > 0)
    .sort((a, b) => b.turnover - a.turnover)
    .slice(0, s.candidates)
    .map((x) => x.it);
}

async function analyse() {
  const token = ++state.runToken;
  const cands = pickCandidates();
  const missing = cands.filter((it) => !state.stats.has(it.id));
  if (missing.length) {
    setStatus(`Fetching 90-day history for ${missing.length} items…`);
    await pool(missing, 6, async (it) => {
      const pts = await loadHistory(it.id, state.market.updated);
      state.stats.set(it.id, historyStats(pts));
    }, (d, t) => token === state.runToken && setProgress(d, t));
  }
  if (token !== state.runToken) return; // a newer run superseded this one
  setProgress(0, 0);
  const c = cfg();
  state.suggestions = cands.flatMap((it) =>
    suggest(it, state.stats.get(it.id), c).map((sg) => ({ ...sg, item: it, st: state.stats.get(it.id) })),
  );
  const failed = cands.filter((it) => !state.stats.get(it.id)).length;
  const when = new Date(state.market.updated).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  setStatus(`GE update ${when} · ${gp(state.market.items.length)} items · ${cands.length} analysed · ${state.suggestions.length} ideas` +
    (failed ? ` · ${failed} without history` : ''));
  renderSuggestions();
  renderFlips();
  return state.suggestions;
}

async function refresh({ notify = false } = {}) {
  try {
    showError('');
    setStatus('Loading Grand Exchange data…');
    const prev = state.market?.updated;
    state.market = await loadMarket();
    state.byId = new Map(state.market.items.map((it) => [it.id, it]));
    if (prev !== state.market.updated) state.stats.clear();
    pruneHistory(state.market.updated);
    const sugg = await analyse();
    if (notify && prev && prev !== state.market.updated) notifyTop(sugg);
  } catch (e) {
    console.warn(e);
    showError(`${e.message}. Check your connection and press “Refresh now”.`);
    setStatus('Offline');
  }
}

// ---------- update polling + notifications ----------
async function poll() {
  state.nextPoll = Date.now() + POLL_MS;
  if (!state.market) return refresh();
  try {
    const latest = await latestUpdateStamp();
    if (latest > state.market.updated) await refresh({ notify: true });
  } catch (e) { console.warn('update check failed', e); }
}
function tickCountdown() {
  const ms = state.nextPoll - Date.now();
  $('#next-check').textContent = ms > 0 ? `Next update check in ${Math.ceil(ms / 60000)} min` : 'Checking…';
}
function notifyTop(sugg) {
  if (!state.settings.notify || !('Notification' in window) || Notification.permission !== 'granted') return;
  const top = [...sugg].sort((a, b) => b.score - a.score).slice(0, 3);
  if (!top.length) return;
  new Notification('New GE update — top flips', {
    body: top.map((s) => `${s.item.name}: buy ${gpC(s.buy)} → sell ${gpC(s.sell)} (≈${gpC(s.total)})`).join('\n'),
    tag: 'rsf-update',
  });
}

// ---------- rendering: suggestions ----------
function sparkline(values, up) {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('class', `spark ${up ? 'up' : 'down'}`);
  svg.setAttribute('viewBox', '0 0 100 30');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('aria-hidden', 'true');
  const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  const pts = values.map((v, i) => `${(i / Math.max(1, values.length - 1)) * 100},${28 - ((v - lo) / span) * 26}`).join(' ');
  const pl = document.createElementNS(NS, 'polyline');
  pl.setAttribute('points', pts);
  pl.setAttribute('stroke', 'currentColor');
  svg.append(pl);
  return svg;
}

function itemCell(it, meta) {
  const img = h('img', { src: iconUrl(it.name), alt: '', loading: 'lazy', width: 28, height: 28 });
  img.addEventListener('error', () => { img.style.visibility = 'hidden'; }, { once: true });
  return h('div', { class: 'item-cell' }, img,
    h('div', {},
      h('a', { href: wikiUrl(it.name), target: '_blank', rel: 'noopener' }, it.name),
      h('span', { class: 'item-meta' },
        h('a', { href: geUrl(it.id), target: '_blank', rel: 'noopener', 'aria-label': `${it.name} on the official GE database` }, 'GE'),
        meta ? ` · ${meta}` : '')));
}

function confidenceCell(sg) {
  const level = sg.confidence >= 0.99 ? ['High', 'up'] : sg.confidence >= 0.5 ? ['Medium', ''] : ['Low', 'down'];
  return h('td', {},
    h('span', { class: `conf ${level[1]}` }, h('span', { class: 'conf-dot', 'aria-hidden': 'true' }), level[0]),
    sg.flags.length ? h('div', { class: 'flags' }, sg.flags.map((f) => h('span', { class: 'badge', 'data-variant': 'outline' }, f))) : null);
}

function renderSuggestions() {
  const q = $('#search').value.trim().toLowerCase();
  const strat = $('#strategy').value;
  const key = $('#sort').value;
  const rows = state.suggestions
    .filter((s) => (strat === 'all' || s.strategy === strat) && (!q || s.item.name.toLowerCase().includes(q)))
    .sort((a, b) => b[key] - a[key]);
  const tbody = $('#sugg-table tbody');
  tbody.replaceChildren(...rows.map((sg) => {
    const { item: it, st } = sg;
    const time = sg.strategy === 'spread' ? `~${sg.hours < 1 ? '<1' : Math.round(sg.hours)} h` : 'days';
    return h('tr', {},
      h('td', {}, itemCell(it, it.members ? 'members' : 'F2P')),
      h('td', { class: 'num' }, gp(it.price),
        h('span', { class: `item-meta ${st.ch1 >= 0 ? 'up' : 'down'}` }, `${st.ch1 >= 0 ? '▲' : '▼'} ${pct(Math.abs(st.ch1))} today`)),
      h('td', { title: `30-day range ${gp(st.min30)} – ${gp(st.max30)}, avg ${gp(st.mean30)}` },
        sparkline(st.spark, st.ch30 >= 0),
        h('span', { class: 'item-meta' }, `30d ${st.ch30 >= 0 ? '+' : ''}${pct(st.ch30)}`)),
      h('td', { class: 'num' }, gpC(it.volume), h('span', { class: 'item-meta' }, `median ${gpC(st.medVol30)}`)),
      h('td', { class: 'num' }, it.limit ? gp(it.limit) : '—'),
      h('td', {}, sg.strategy === 'spread' ? 'Spread flip' : 'Dip recovery', h('span', { class: 'item-meta' }, time)),
      h('td', { class: 'num' }, gp(sg.buy)),
      h('td', { class: 'num' }, gp(sg.sell), h('span', { class: 'item-meta' }, `tax ${gp(geTax(sg.sell, cfg().taxRate, it.name))}`)),
      h('td', { class: 'num up' }, gp(sg.profit)),
      h('td', { class: 'num' }, gp(sg.qty), h('span', { class: 'item-meta' }, `${gpC(sg.qty * sg.buy)} gp`)),
      h('td', { class: 'num' }, h('strong', {}, gpC(sg.total))),
      h('td', { class: 'num' }, pct(sg.roi, 2)),
      confidenceCell(sg),
      h('td', {}, h('button', { class: 'btn', 'data-size': 'sm', 'data-variant': 'outline', type: 'button', onclick: () => openMargin(sg) }, 'Check margin')),
    );
  }));
  $('#sugg-empty').classList.toggle('hidden', rows.length > 0);
}

// ---------- margin check dialog ----------
let marginCtx = null;
function openMargin(sg) {
  marginCtx = sg;
  const f = $('#margin-form');
  f.reset();
  f.buy.value = sg.buy; f.sell.value = sg.sell; f.qty.value = sg.qty;
  $('#margin-sub').textContent = `${sg.item.name} · guide ${gp(sg.item.price)} · buy limit ${sg.item.limit ? gp(sg.item.limit) : 'unknown'} per 4 h`;
  updateMargin();
  $('#margin-dialog').showModal();
  f.instaBuy.focus();
}
function readGp(input) { return parseGp(input.value); }
function updateMargin(e) {
  const f = $('#margin-form');
  const ib = readGp(f.instaBuy), is = readGp(f.instaSell);
  // Typing the margin-check results proposes offers 1 gp inside the spread.
  if (e && (e.target === f.instaBuy || e.target === f.instaSell) && ib > 0 && is > 0) {
    f.buy.value = Math.min(ib, is) + 1;
    f.sell.value = Math.max(ib, is) - 1;
  }
  const buy = readGp(f.buy), sell = readGp(f.sell), qty = Number(f.qty.value);
  const out = $('#margin-out');
  if (!(buy > 0 && sell > 0 && qty > 0)) { out.replaceChildren(h('span', {}, 'Enter prices to see the result.')); return; }
  const rate = cfg().taxRate, name = marginCtx.item.name;
  const tax = geTax(sell, rate, name), per = netSell(sell, rate, name) - buy;
  const lim = marginCtx.item.limit;
  const rowsOut = [
    ib > 0 && is > 0 ? ['Live margin (before tax)', gp(Math.abs(ib - is))] : null,
    ['Tax per item', gp(tax)],
    ['Break-even sell price', gp(breakEvenSell(buy, rate, name))],
    ['Profit per item', signed(per), 'pl'],
    ['Cash needed', `${gp(buy * qty)} gp`],
    ['ROI', pct(per / buy, 2), 'pl'],
    ['Total profit', signed(per * qty), 'pl big'],
  ].filter(Boolean);
  out.replaceChildren(
    ...rowsOut.flatMap(([k, v, cls]) => [h('dt', {}, k), h('dd', { class: cls ? `${cls} ${per > 0 ? 'up' : 'down'}` : '' }, v)]),
    ...[
      lim && qty > lim ? `Over the buy limit: only ${gp(lim)} fill per 4 hours.` : null,
      buy * qty > state.settings.budget ? `More than your ${gpC(state.settings.budget)} cash per item.` : null,
      per <= 0 ? 'Not profitable after tax — skip it or widen the offers.' : null,
    ].filter(Boolean).map((t) => h('p', { class: 'down', style: 'grid-column:1/-1' }, t)),
  );
}
$('#margin-form').addEventListener('input', updateMargin);
$('#margin-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const buy = readGp(f.buy), sell = readGp(f.sell), qty = Number(f.qty.value);
  for (const [input, ok] of [[f.buy, buy > 0], [f.sell, sell > buy], [f.qty, qty >= 1]]) {
    input.setCustomValidity(ok ? '' : input === f.sell ? 'Sell must be above buy' : 'Enter a positive number');
  }
  if (!f.reportValidity()) return;
  state.flips.unshift({
    uid: crypto.randomUUID(), itemId: marginCtx.item.id, name: marginCtx.item.name,
    qty, buy, sell, started: Date.now(), status: 'open',
  });
  saveFlips(state.flips);
  $('#margin-dialog').close();
  renderFlips();
  $('#flips-h').scrollIntoView({ behavior: 'smooth' });
});
$('#margin-form').addEventListener('input', (e) => e.target.setCustomValidity?.(''));

// ---------- flips ----------
let sellCtx = null;
function openSell(flip) {
  sellCtx = flip;
  const f = $('#sell-form');
  f.sold.value = flip.sell; f.qty.value = flip.qty;
  $('#sell-sub').textContent = `${flip.name} · bought ${gp(flip.qty)} at ${gp(flip.buy)}`;
  updateSell();
  $('#sell-dialog').showModal();
  f.sold.focus();
}
function updateSell() {
  const f = $('#sell-form');
  const sold = readGp(f.sold), qty = Number(f.qty.value);
  const out = $('#sell-out');
  if (!(sold > 0) || !(qty >= 0)) { out.replaceChildren(); return; }
  const p = (netSell(sold, cfg().taxRate, sellCtx.name) - sellCtx.buy) * qty;
  out.replaceChildren(h('dt', {}, 'Realised profit'), h('dd', { class: `big ${p >= 0 ? 'up' : 'down'}` }, signed(p)));
}
$('#sell-form').addEventListener('input', updateSell);
$('#sell-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const sold = readGp(f.sold), qty = Number(f.qty.value);
  f.sold.setCustomValidity(sold > 0 ? '' : 'Enter a price');
  f.qty.setCustomValidity(qty >= 0 && qty <= sellCtx.qty ? '' : `0 – ${sellCtx.qty}`);
  if (!f.reportValidity()) return;
  Object.assign(sellCtx, {
    status: 'closed', sold, soldQty: qty, closed: Date.now(),
    // Profit on the units actually sold; unsold units are simply still in your bank.
    profit: (netSell(sold, cfg().taxRate, sellCtx.name) - sellCtx.buy) * qty,
  });
  saveFlips(state.flips);
  $('#sell-dialog').close();
  renderFlips();
});
$('#sell-form').addEventListener('input', (e) => e.target.setCustomValidity?.(''));

const ago = (t) => {
  const m = Math.round((Date.now() - t) / 60000);
  return m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
};
function renderFlips() {
  const rate = cfg().taxRate;
  const tbody = $('#flips-table tbody');
  tbody.replaceChildren(...state.flips.map((fl) => {
    const now = state.byId.get(fl.itemId)?.price;
    const open = fl.status === 'open';
    const projected = (netSell(fl.sell, rate, fl.name) - fl.buy) * fl.qty;
    return h('tr', {},
      h('td', {}, itemCell({ id: fl.itemId, name: fl.name }, open ? 'open' : 'finished')),
      h('td', {}, ago(fl.started)),
      h('td', { class: 'num' }, gp(fl.qty)),
      h('td', { class: 'num' }, gp(fl.buy)),
      h('td', { class: 'num' }, gp(fl.sell)),
      h('td', { class: 'num' }, now ? gp(now) : '—'),
      h('td', { class: `num ${(open ? projected : fl.profit) >= 0 ? 'up' : 'down'}` },
        open ? h('span', { title: 'If sold at target, after tax' }, `${signed(projected)} (target)`)
          : h('strong', {}, signed(fl.profit))),
      h('td', {}, h('div', { class: 'flex gap-1 justify-end' },
        open ? h('button', { class: 'btn', 'data-size': 'sm', type: 'button', onclick: () => openSell(fl) }, 'Record sale') : null,
        h('button', {
          class: 'btn', 'data-size': 'sm', 'data-variant': 'ghost', type: 'button', 'aria-label': `Delete ${fl.name} flip`,
          onclick: () => { if (confirm(`Delete the ${fl.name} flip?`)) { state.flips = state.flips.filter((x) => x !== fl); saveFlips(state.flips); renderFlips(); } },
        }, 'Delete'))),
    );
  }));
  $('#flips-empty').classList.toggle('hidden', state.flips.length > 0);
  const closed = state.flips.filter((f) => f.status === 'closed');
  const openFl = state.flips.filter((f) => f.status === 'open');
  const realised = closed.reduce((s, f) => s + f.profit, 0);
  const tied = openFl.reduce((s, f) => s + f.buy * f.qty, 0);
  $('#flip-totals').textContent = state.flips.length
    ? `Realised: ${signed(realised)} gp over ${closed.length} flips · ${gp(tied)} gp in ${openFl.length} open`
    : '';
}
$('#clear-closed').addEventListener('click', () => {
  if (!state.flips.some((f) => f.status === 'closed') || !confirm('Remove all finished flips from the log?')) return;
  state.flips = state.flips.filter((f) => f.status === 'open');
  saveFlips(state.flips);
  renderFlips();
});

// ---------- settings form ----------
const form = $('#settings');
function fillSettings() {
  const s = state.settings;
  form.budget.value = gpC(s.budget).toLowerCase();
  for (const k of ['taxPct', 'minPrice', 'minVolume', 'capturePct', 'candidates', 'bandK', 'dipZ']) form[k].value = s[k];
  form.members.checked = s.members;
  form.notify.checked = s.notify;
}
let debounce;
form.addEventListener('input', () => {
  const s = { ...state.settings };
  const budget = parseGp(form.budget.value);
  form.budget.setCustomValidity(budget > 0 ? '' : 'Use a number like 50m or 250k');
  if (budget > 0) s.budget = budget;
  for (const k of ['taxPct', 'minPrice', 'minVolume', 'capturePct', 'candidates', 'bandK', 'dipZ']) {
    const v = form[k].valueAsNumber;
    if (Number.isFinite(v) && form[k].checkValidity()) s[k] = v;
  }
  s.members = form.members.checked;
  state.settings = s;
  saveSettings(s);
  clearTimeout(debounce);
  if (state.market) debounce = setTimeout(analyse, 400);
});
form.notify.addEventListener('change', async () => {
  if (!form.notify.checked) return;
  if (!('Notification' in window)) { form.notify.checked = false; alert('This browser has no notifications.'); }
  else if ((await Notification.requestPermission()) !== 'granted') { form.notify.checked = false; alert('Notifications are blocked for this page.'); }
  state.settings = { ...state.settings, notify: form.notify.checked };
  saveSettings(state.settings);
});
form.addEventListener('submit', (e) => e.preventDefault());

// ---------- misc wiring ----------
for (const id of ['#search', '#strategy', '#sort']) $(id).addEventListener('input', renderSuggestions);
$('#refresh').addEventListener('click', () => refresh({ notify: true }));
for (const d of document.querySelectorAll('dialog')) {
  d.addEventListener('click', (e) => { if (e.target === d || e.target.closest('[data-close]')) d.close(); });
}

fillSettings();
renderFlips();
state.nextPoll = Date.now() + POLL_MS;
await refresh();
setInterval(poll, POLL_MS);
setInterval(tickCountdown, 15000);
tickCountdown();
