// RS3 Desk console: a Grand Exchange-style 3 x 3 grid. Each slot card shows the plan for that slot
// and has its own "what happened" buttons; the 9th cell holds the check-in choice and Save & refresh.
const fmt = new Intl.NumberFormat('en-US');
const $ = (s, el = document) => el.querySelector(s);
const SPRITE = (id) => `https://secure.runescape.com/m=itemdb_rs/obj_sprite.gif?id=${id}`;  // the game's own item image

let state = null;
let busy = false;
const picks = new Map();   // trade number -> {kind, qty?, price?, sell_price?}

function h(tag, cls, ...kids) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  for (const k of kids.flat()) if (k != null && k !== false) el.append(k instanceof Node ? k : String(k));
  return el;
}
const n0 = (v) => fmt.format(Math.round(v));
function short(v) {
  const a = Math.abs(v);
  const s = a >= 1e6 ? `${(a / 1e6).toFixed(a >= 1e7 ? 1 : 2)}m` : a >= 1e3 ? `${Math.round(a / 1e3)}k` : `${a}`;
  return (v < 0 ? '−' : '+') + s;
}

async function api(path, body) {
  const res = await fetch(path, body === undefined ? { cache: 'no-store' } : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || res.statusText);
  return data;
}

// ---------- report choices per offer
const LABELS = {
  done: (r) => (r.side === 'sell' ? 'Sold' : 'Bought'),
  done_sold: () => 'Bought + sold',
  unlisted: () => 'Bought, not listed',
  part: (r) => (r.side === 'sell' ? 'Part sold' : 'Part bought'),
  aborted: () => 'Aborted',
  not_placed: () => 'Not placed',
  price: () => 'Other price',
  no_reprice: () => 'Re-price not done',
  keep: () => 'Keep price',
  not_aborted: () => 'Not aborted',
  more_filled: () => 'More had filled',
};
// the default chip: "I did what the plan said" (nothing to report)
const SUGGESTED = (r) => ({
  BUY: 'Placed', SELL: 'Listed', CHANGE: 'Re-priced', KEEP: 'No change',
  ABORT: r.sell_n ? 'Aborted & listed' : 'Aborted',
}[r.verb] || 'As planned');
const TIPS = {
  done: (r) => (r.side === 'sell' ? 'All sold' : r.machine ? 'All bought' : `All bought and listed at ${n0(r.then_sell || 0)}`),
  done_sold: () => 'All bought and already sold too',
  unlisted: () => 'All bought, in your backpack, not listed yet',
  part: () => 'Partly filled, still in the GE: enter the total so far',
  aborted: () => 'You aborted it: enter how many had filled',
  not_placed: () => 'You never placed this offer',
  price: () => 'It is listed at a different price: enter it',
  no_reprice: (r) => `You didn't do the re-price: it is still at ${n0(r.was_price)}`,
  keep: () => 'Keep this price: stop suggesting changes',
  not_aborted: (r) => `You didn't abort it: the ${r.side} is still in the GE` + (r.sell_n ? ' (and the sell is not listed)' : ''),
  more_filled: () => 'You aborted it, but more had filled: enter the total',
};

function choicesFor(r) {
  if (r.aborted) return ['not_aborted', 'more_filled'];
  const k = ['done'];
  if (r.side === 'buy' && !r.machine) k.push('done_sold', 'unlisted');
  k.push('part', 'aborted');
  if (['BUY', 'SELL', 'CHANGE'].includes(r.verb)) k.push('not_placed');
  k.push('price');
  if (r.verb === 'CHANGE' && r.was_price) k.push('no_reprice');
  if (!r.machine && !r.pinned) k.push('keep');
  return k;
}

function reportBox(open, card) {
  const box = h('div', 'ge-report' + (open.aborted ? ' abort' : ''));
  box.append(h('span', 'lbl', open.aborted ? 'The abort:' : 'What happened?'));
  const chips = h('div', 'ge-chips');
  const extra = h('div', 'ge-extra');
  extra.hidden = true;
  const input = h('input', 'ge-num');
  input.type = 'number'; input.min = '0';
  const xlabel = h('label', null);
  const id = `x-${open.n}`;
  input.id = id; xlabel.htmlFor = id;
  extra.append(xlabel, input);

  const sync = () => {
    const p = picks.get(open.n);
    for (const b of chips.children) b.setAttribute('aria-pressed', String((p?.kind ?? 'suggested') === b.dataset.kind));
    card.classList.toggle('reported', [...picks.values()].some((x) => x.card === card.dataset.slot));
    const need = p && { part: 'qty', aborted: 'qty', price: 'price', done_sold: 'sell_price', not_aborted: 'qty', more_filled: 'qty' }[p.kind];
    extra.hidden = !need;
    if (need) {
      xlabel.textContent = p.kind === 'more_filled' ? 'Bought in total'
        : p.kind === 'not_aborted' ? 'Filled so far (optional)'
        : { qty: open.side === 'sell' ? 'Sold so far' : 'Bought so far', price: 'Listed at', sell_price: 'Sold at' }[need];
      input.placeholder = need === 'qty' ? n0(open.filled) : need === 'price' ? n0(open.price) : n0(open.then_sell || 0);
      input.max = need === 'qty' ? String(open.qty) : '';
      input.value = p[need] ?? '';
      input.dataset.field = need;
    }
    updatePending();
  };
  const def = h('button', 'ge-chip suggested', `✓ ${SUGGESTED(open)}`);
  def.type = 'button';
  def.dataset.kind = 'suggested';
  def.title = 'Done as the plan said (default)';
  def.addEventListener('click', () => { picks.delete(open.n); sync(); });
  chips.append(def);
  for (const kind of choicesFor(open)) {
    const b = h('button', 'ge-chip', LABELS[kind](open));
    b.type = 'button';
    b.dataset.kind = kind;
    b.title = TIPS[kind](open);
    b.addEventListener('click', () => {
      if (picks.get(open.n)?.kind === kind) picks.delete(open.n);
      else picks.set(open.n, { n: open.n, kind, card: card.dataset.slot });
      sync();
      if (!extra.hidden) input.focus();
    });
    chips.append(b);
  }
  input.addEventListener('input', () => {
    const p = picks.get(open.n);
    if (!p) return;
    const v = input.value === '' ? undefined : Number(input.value);
    p[input.dataset.field] = v;
  });
  box.append(chips, extra);
  queueMicrotask(sync);
  return box;
}

// ---------- slot cards
function entry(r, open, card) {
  const body = [];
  const icon = h('div', 'ge-icon');
  if (r.item_id) {
    const img = h('img');
    img.src = SPRITE(r.item_id); img.alt = ''; img.loading = 'lazy';
    img.addEventListener('error', () => img.remove());
    icon.append(img);
  }
  icon.append(h('span', 'q', n0(r.of ?? r.qty ?? 0)));
  const copy = h('button', 'ge-copy', '⧉ copy');
  copy.type = 'button';
  copy.title = 'Copy the item name for the GE search';
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(r.item); copy.textContent = '✓ copied'; } catch { copy.textContent = 'copy failed'; }
    setTimeout(() => { copy.textContent = '⧉ copy'; }, 1500);
  });
  const name = h('div', 'ge-name', h('strong', null, (r.star ? '★ ' : '') + r.item), h('small', null, `#${r.n}`), copy);
  name.firstChild.title = r.item;
  // big quantity right of the name: what's left to trade on this offer (filled shown on the bar)
  const left = r.qty ?? Math.max(0, (r.of ?? 0) - (r.filled ?? 0));
  const qbox = h('div', 'ge-qty', h('span', 'qn', n0(left)), h('span', 'ql', r.of && left !== r.of ? `left of ${n0(r.of)}` : 'qty'));
  qbox.title = `${n0(left)} ${r.side === 'sell' ? 'to sell' : 'to buy'}`;
  body.push(h('div', 'ge-item', icon, name, qbox));

  // two zones: BUY on the left, SELL on the right, each only when it applies
  const zones = h('div', 'ge-zones');
  const cp = (r.changes || []).find((c) => c.what === 'price');
  const zone = (side, price, opts = {}) => {
    const z = h('div', `ge-zone ${side}` + (opts.rp ? ' rp' : '') + (opts.next ? ' next' : ''));
    z.append(h('span', 'zl', side === 'buy' ? 'BUY' : 'SELL'), h('span', 'zp', n0(price)));
    const sub = h('div', 'zs');
    if (opts.rp && r.was_price != null) {
      const d = price - r.was_price;
      sub.append(h('s', null, n0(r.was_price)), ' ', h('b', 'zd', `${d < 0 ? '−' : '+'}${n0(Math.abs(d))} each`));
    } else if (opts.was != null) sub.append(h('s', null, n0(opts.was)));
    if (opts.total != null) sub.append(h('span', null, `GE total ${n0(opts.total)}`));
    if (opts.next) sub.append(h('span', null, 'once bought'));
    if (sub.childNodes.length) z.append(sub);
    z.style.gridColumn = side === 'buy' ? '1' : '2';
    return z;
  };
  const showTotal = ['BUY', 'SELL', 'CHANGE'].includes(r.verb) ? r.ge_total : null;
  if (r.price != null) {
    zones.append(zone(r.side, r.price, { rp: r.verb === 'CHANGE', was: cp ? cp.from : null, total: showTotal }));
  }
  if (r.side === 'buy' && r.then_sell != null) zones.append(zone('sell', r.then_sell, { next: true }));
  body.push(zones);

  const meta = h('div', 'ge-meta');
  if (r.est && !r.est.made) meta.append(h('span', 'ge-est' + (r.est.value < 0 ? ' neg' : ''), `Est. ${short(r.est.value)}`));
  if (r.trait) meta.append(h('span', null, r.trait));
  if (meta.childNodes.length) body.push(meta);

  if (r.of) {
    const bar = h('div', 'ge-bar-wrap' + (r.filled >= r.of ? ' done' : ''));
    const fill = h('i'); fill.style.width = `${Math.min(100, Math.round((r.filled / r.of) * 100))}%`;
    bar.append(fill, h('span', null, `${n0(r.filled)} / ${n0(r.of)}`));
    body.push(bar);
  }

  if (r.verb === 'CHANGE') {
    body.push(h('div', 'ge-note rp', `RE-PRICE: abort and re-list ${n0(r.qty)} at ${n0(r.price)}`));
  } else if (r.verb === 'BUY' || r.verb === 'SELL') {
    if (r.note && r.note !== 'listed since the plan') body.push(h('div', 'ge-note', r.note));
  } else if (r.note && r.verb !== 'KEEP') body.push(h('div', 'ge-note', r.note));
  for (const c of r.changes || []) {
    if (c.what === 'new' && !card?.dataset.hasAbort) body.push(h('div', 'ge-note chg', c.from ? `New offer here (was ${c.from})` : 'New offer in an empty slot'));
    if (c.what === 'slot') body.push(h('div', 'ge-note chg', `Moved from slot ${c.from}`));
    if (c.what === 'qty') body.push(h('div', 'ge-note chg', `Quantity ${n0(c.from)} → ${n0(c.to)}`));
  }
  if (open) body.push(reportBox(open, card));
  return body;
}

function pastLine(r) {
  const cancel = (r.changes || []).find((c) => c.what === 'cancel');
  if (cancel) return h('div', 'ge-note abort', `✕ ABORT ${r.side} ${r.item} @ ${n0(r.price)}` + (r.filled ? `, collect ${n0(r.filled)}` : ''));
  const done = r.state === 'done';
  const line = h('div', 'ge-past', `${done ? '✓ Finished' : '✕ Closed'}: ${r.side} ${n0(r.filled)}/${n0(r.of)} ${r.item}${done ? ', collect it' : ''}`);
  if (r.est && r.est.made) line.append(' ', h('b', r.est.value < 0 ? 'neg' : '', short(r.est.value)));
  return line;
}

function reserveToggle(slot, on) {
  const lab = h('label', 'ge-reserve');
  const box = h('input');
  box.type = 'checkbox'; box.checked = on;
  box.setAttribute('aria-label', `Slot ${slot} reserved for your own offer`);
  box.addEventListener('change', async () => {
    box.disabled = true;
    try { await api('/api/reserve', { slot, on: box.checked }); lastJson = ''; await load(); }
    catch (e) { box.checked = !box.checked; box.disabled = false; setStatus(`Error: ${e.message}`, true); }
  });
  lab.append(box, ' Reserved');
  lab.title = on ? 'Your own offer is here: the planner skips this slot. Untick to flip in it again.'
                 : 'Tick if you put your own offer in this slot: the planner will skip it.';
  return lab;
}

function slotCard(slot, rows, openByN, reserved = false) {
  const li = $('#slot-tpl').content.firstElementChild.cloneNode(true);
  const card = $('.ge-card', li);
  const live = rows.filter((r) => !r.state || r.state === 'open');
  const finished = rows.filter((r) => r.state && r.state !== 'open');
  const main = live.at(-1);
  const any = main || rows.at(-1);
  $('.ge-title', li).textContent = any ? `${slot} - ${any.side === 'sell' ? 'Sell' : 'Buy'}` : `Slot ${slot}`;
  const badges = $('.ge-badges', li);
  if (main) {
    const verb = main.verb === 'CHANGE' ? 'RE-PRICE' : main.verb === 'BUY' || main.verb === 'SELL' ? 'NEW' : main.verb;
    badges.append(h('span', `ge-badge verb-${main.verb}`, verb));
    if (main.pinned) badges.append(h('span', 'ge-badge lock', '🔒'));
    if (main.machine) badges.append(h('span', 'ge-badge lock', '⚙ machine'));
  }
  if (rows.some((r) => r.changes && r.changes.length)) badges.append(h('span', 'ge-badge chg', 'CHANGE'));
  badges.append(reserveToggle(slot, reserved));
  if (reserved) card.classList.add('reserved');
  if (main?.verb === 'CHANGE') card.classList.add('reprice');
  else if (rows.some((r) => r.changes && r.changes.length)) card.classList.add('changed');
  if (!main) card.classList.add('idle');

  card.dataset.slot = String(slot);
  const body = $('.ge-body', li);
  finished.forEach((r) => {
    body.append(pastLine(r));
    const ab = r.verb === 'ABORT' && r.num != null ? openByN.get(r.num) : null;
    if (ab) { body.append(reportBox(ab, card)); card.dataset.hasAbort = '1'; }
  });
  if (main) body.append(...entry(main, openByN.get(main.num), card));
  else if (!finished.length) body.append(h('p', 'ge-empty', reserved ? 'Reserved: your own offer' : 'Empty'));
  return li;
}

function render() {
  const grid = $('#grid');
  const keepFocus = document.activeElement?.id;
  grid.replaceChildren();
  const b = state.board;
  const openByN = new Map(state.rows.filter((r) => r.n != null).map((r) => [r.n, r]));
  if (b) {
    const t = new Date(b.at);
    $('#plan-when').textContent = `${b.title} · made ${t.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}`;
  }
  const bySlot = new Map();
  for (const r of b?.rows || []) {
    const k = r.slot || 0;
    if (!bySlot.has(k)) bySlot.set(k, []);
    bySlot.get(k).push(r);
  }
  const reserved = new Set(state.reserved || []);
  for (let s = 1; s <= 8; s++) grid.append(slotCard(s, bySlot.get(s) || [], openByN, reserved.has(s)));
  grid.append(panel());
  const loose = bySlot.get(0) || [];
  if (loose.length) $('#status').textContent = `Not in a slot: ${loose.map((r) => r.item).join(', ')}`;
  if (keepFocus && document.getElementById(keepFocus)) document.getElementById(keepFocus).focus();
}

// ---------- panel (9th cell)
let panelEl = null;
let whenState = { mode: 'normal', hours: '2', at: '07:00', days: '2' };
function panel() {
  if (panelEl) { fillStats(); return panelEl; }
  panelEl = $('#panel-tpl').content.firstElementChild.cloneNode(true);
  const root = panelEl;
  root.addEventListener('change', (e) => {
    if (e.target.name === 'mode') whenState.mode = e.target.value;
    else if (e.target.name in whenState) whenState[e.target.name] = e.target.value;
  });
  // typing in a field selects its option
  for (const [field, mode] of [['hours', 'hours'], ['at', 'at'], ['days', 'away']]) {
    $(`[name=${field}]`, root).addEventListener('focus', () => {
      $(`input[name=mode][value=${mode}]`, root).checked = true; whenState.mode = mode;
    });
  }
  $('#btn-refresh', root).addEventListener('click', () => submit(true));
  $('#btn-save', root).addEventListener('click', () => submit(false));
  $('#btn-totals', root).addEventListener('click', () => showText('Totals', '/api/totals'));
  $('#btn-machines', root).addEventListener('click', () => showText('Machines', '/api/machines', true));
  $('#history', root).addEventListener('change', (e) => {
    if (e.target.value) showPlan(e.target.value, e.target.selectedOptions[0].textContent);
    e.target.value = '';
  });
  fillStats();
  return root;
}

function fillStats() {
  const root = panelEl;
  $('#st-cash', root).textContent = short(state.cash).replace('+', '');
  $('#st-pl', root).textContent = short(state.realised);
  $('#st-stock', root).textContent = short(state.stock).replace('+', '');
  const hist = $('#history', root);
  hist.replaceChildren(h('option', null, 'Earlier plans…'));
  hist.firstChild.value = '';
  for (const p of state.recent || []) {
    const o = h('option', null, new Date(p.at).toLocaleString('en-GB', { weekday: 'short', hour: '2-digit', minute: '2-digit' }));
    o.value = p.id;
    hist.append(o);
  }
  updatePending();
}

function updatePending() {
  if (!panelEl) return;
  $('#st-pending', panelEl).textContent = picks.size ? `${picks.size} slot${picks.size > 1 ? 's' : ''}` : 'none';
}

function setStatus(msg, err = false) {
  const el = $('#status', panelEl);
  el.textContent = msg;
  el.classList.toggle('err', err);
}

function collect() {
  const out = [];
  for (const p of picks.values()) {
    const open = state.rows.find((r) => r.n === p.n);
    const what = open ? open.item : `#${p.n}`;
    if ((p.kind === 'part' || p.kind === 'aborted') && p.qty == null) throw new Error(`${what}: enter how many filled`);
    if (p.kind === 'price' && p.price == null) throw new Error(`${what}: enter the price it's listed at`);
    const { card, ...send } = p;
    out.push(send);
  }
  return out;
}

function when() {
  const m = whenState.mode;
  return { mode: m, value: { hours: whenState.hours, at: whenState.at, away: whenState.days }[m] };
}

async function submit(refresh) {
  let actions;
  try { actions = collect(); } catch (e) { setStatus(e.message, true); return; }
  if (!refresh && !actions.length) { setStatus('Nothing to save.'); return; }
  busy = true;
  const btns = [$('#btn-refresh', panelEl), $('#btn-save', panelEl)];
  btns.forEach((b) => { b.disabled = true; });
  setStatus(refresh ? 'Saving and building the new plan (30–60 s)…' : 'Saving…');
  try {
    await api('/api/report', { actions, refresh, when: when() });
    picks.clear();
    await load();
    setStatus(refresh ? 'New plan ready.' : 'Saved.');
  } catch (e) {
    setStatus(`Error: ${e.message}`, true);
    picks.clear();
    await load().catch(() => {});
  } finally {
    busy = false;
    btns.forEach((b) => { b.disabled = false; });
  }
}

// ---------- dialogs
function openDialog(title) {
  $('#dlg-h').textContent = title;
  $('#dlg-body').hidden = false;
  $('#dlg-body').textContent = 'Loading…';
  $('#dlg-frame').hidden = true;
  $('#bank').hidden = true;
  $('#dlg').showModal();
}
async function showText(title, path, bank = false) {
  openDialog(title);
  $('#bank').hidden = !bank;
  try { $('#dlg-body').textContent = (await api(path)).text; } catch (e) { $('#dlg-body').textContent = `Error: ${e.message}`; }
}
function showPlan(id, label) {
  openDialog(`Earlier plan: ${label}`);
  $('#dlg-body').hidden = true;
  const f = $('#dlg-frame');
  f.hidden = false;
  f.src = `/plans/${id}?t=${Date.now()}`;
}
$('#dlg-close').addEventListener('click', () => $('#dlg').close());
$('#bank').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    $('#dlg-body').textContent = (await api('/api/bank', { kind: f.kind.value, item: f.item.value.trim(), qty: Number(f.qty.value) })).text;
    f.reset();
  } catch (err) { $('#dlg-body').textContent = `Error: ${err.message}`; }
});

// ---------- load + light polling (never while you have unsaved choices)
let lastJson = '';
async function load() {
  const next = await api('/api/state');
  const json = JSON.stringify(next);
  if (json === lastJson) return;  // nothing changed: don't redraw (keeps icons and focus steady)
  lastJson = json;
  state = next;
  for (const n of picks.keys()) if (!state.rows.some((r) => r.n === n)) picks.delete(n);
  render();
}
setInterval(() => {
  if (!busy && !picks.size && !$('#dlg').open && document.visibilityState === 'visible') load().catch(() => {});
}, 20000);

load().catch((e) => { $('#grid').replaceChildren(h('li', 'ge-empty', `Can't reach the desk server: ${e.message}`)); });
