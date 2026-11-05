// RS3 Desk console: report what happened per open offer, then save and refresh the plan.
const fmt = new Intl.NumberFormat('en-US');
const $ = (s, el = document) => el.querySelector(s);
let state = null;

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || res.statusText);
  return data;
}

function gp(n) {
  const a = Math.abs(n);
  const s = a >= 1e6 ? `${(a / 1e6).toFixed(1)}m` : a >= 1e3 ? `${(a / 1e3).toFixed(1)}k` : `${a}`;
  return (n < 0 ? '−' : '') + s;
}

function choicesFor(r) {
  const k = ['same', 'done'];
  if (r.side === 'buy' && !r.machine) k.push('done_sold', 'unlisted');
  k.push('part', 'aborted');
  if (['BUY', 'SELL', 'CHANGE'].includes(r.verb)) k.push('not_placed');
  k.push('price');
  if (r.verb === 'CHANGE' && r.was_price) k.push('no_reprice');
  if (!r.machine && !r.pinned) k.push('keep');
  return k;
}

function label(kind, r) {
  const L = { ...state.actions };
  if (r.side === 'sell') { L.done = 'Sold (all)'; L.part = 'Partly sold, still in the GE'; L.aborted = 'Aborted (enter how many sold)'; }
  else if (r.machine) L.done = 'Bought (all)';
  else L.done = `Bought, listed at ${fmt.format(r.then_sell || 0)}`;
  if (kind === 'no_reprice') return `Re-price not done (still at ${fmt.format(r.was_price)})`;
  return L[kind];
}

function render() {
  const s = state;
  $('#summary').textContent = s.plan
    ? `Plan ${s.plan} · cash ${gp(s.cash)} · realised P/L ${s.realised >= 0 ? '+' : ''}${gp(s.realised)}`
    : 'No plan yet';
  const list = $('#rows');
  list.replaceChildren();
  const tpl = $('#row-tpl');
  for (const r of s.rows) {
    if (r.n == null) continue;
    const li = tpl.content.firstElementChild.cloneNode(true);
    li.dataset.n = r.n;
    $('.slot', li).textContent = r.slot ? `Slot ${r.slot}` : '—';
    const side = $('.side', li);
    side.textContent = r.side.toUpperCase();
    side.classList.add(r.side);
    $('.itm', li).textContent = r.item + (r.machine ? ' ⚙' : '') + (r.pinned ? ' 🔒' : '');
    $('.det', li).textContent = `${fmt.format(r.filled)}/${fmt.format(r.qty)} @ ${fmt.format(r.price)}`
      + (r.verb === 'CHANGE' && r.was_price ? ` (re-price from ${fmt.format(r.was_price)})` : '')
      + (r.then_sell ? ` · then sell @ ${fmt.format(r.then_sell)}` : '');
    const sel = $('.kind', li);
    const id = `kind-${r.n}`;
    sel.id = id;
    $('[data-for="kind"]', li).htmlFor = id;
    $('[data-for="kind"]', li).textContent = `What happened to ${r.item}`;
    for (const k of choicesFor(r)) sel.add(new Option(label(k, r), k));
    $('.price', li).placeholder = fmt.format(r.price);
    $('.qty', li).max = r.qty;
    $('.qty', li).placeholder = `${fmt.format(r.filled)}`;
    $('.sell', li).placeholder = r.then_sell ? fmt.format(r.then_sell) : '';
    sel.addEventListener('change', () => toggle(li, r));
    list.append(li);
  }
  if (!list.children.length) list.append(Object.assign(document.createElement('li'), { className: 'muted', textContent: 'No open offers.' }));
}

function toggle(li, r) {
  const k = $('.kind', li).value;
  const show = (cls, on, text) => {
    const w = $(cls, li);
    w.hidden = !on;
    if (text) $('.lbl', w).textContent = text;
  };
  show('.qty-wrap', k === 'part' || k === 'aborted', r.side === 'sell' ? 'Sold so far' : 'Bought so far');
  show('.price-wrap', ['done', 'done_sold', 'part', 'aborted', 'price', 'keep'].includes(k),
    k === 'price' ? 'Listed at' : k === 'keep' ? 'Price (if different)' : 'Price each (if different)');
  show('.sell-wrap', k === 'done_sold');
  li.classList.toggle('changed', k !== 'same');
}

function collect() {
  const out = [];
  for (const li of $('#rows').children) {
    const kind = $('.kind', li)?.value;
    if (!kind || kind === 'same') continue;
    const num = (sel) => { const v = $(sel, li).value; return v === '' ? null : Number(v); };
    const a = { n: Number(li.dataset.n), kind };
    const qty = num('.qty'), price = num('.price'), sell = num('.sell');
    if (qty != null) a.qty = qty;
    if (price != null) a.price = price;
    if (sell != null) a.sell_price = sell;
    if ((kind === 'part' || kind === 'aborted') && a.qty == null) throw new Error(`Enter a quantity for ${$('.itm', li).textContent}`);
    if (kind === 'price' && a.price == null) throw new Error(`Enter the listed price for ${$('.itm', li).textContent}`);
    out.push(a);
  }
  return out;
}

function when() {
  const f = $('#report');
  const mode = f.mode.value;
  const value = { hours: f.hours.value, at: f.at.value, away: f.days.value }[mode];
  return { mode, value };
}

function setStatus(msg, err = false) {
  const el = $('#status');
  el.textContent = msg;
  el.classList.toggle('err', err);
}

async function submit(refresh) {
  let actions;
  try { actions = collect(); } catch (e) { setStatus(e.message, true); return; }
  if (!refresh && !actions.length) { setStatus('Nothing to save.'); return; }
  const btns = [$('#btn-refresh'), $('#btn-save')];
  btns.forEach((b) => { b.disabled = true; });
  setStatus(refresh ? 'Saving and building the new plan (about 30–60 s)…' : 'Saving…');
  try {
    const res = await api('/api/report', { actions, refresh, when: when() });
    $('#out').textContent = [...res.log, res.plan || ''].filter(Boolean).join('\n\n');
    $('#out-wrap').hidden = false;
    setStatus(refresh ? 'New plan ready. The board on the left is updated.' : 'Saved.');
    await load();
  } catch (e) {
    setStatus(`Error: ${e.message}`, true);
    await load();
  } finally {
    btns.forEach((b) => { b.disabled = false; });
  }
}

function renderHistory() {
  const pick = $('#board-pick');
  const keep = pick.value;
  pick.replaceChildren(new Option('Current board (live)', '/board'));
  for (const p of state.recent || []) {
    const t = new Date(p.at);
    const when = t.toLocaleString('en-GB', { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    pick.add(new Option(`Earlier plan: ${when}`, `/plans/${p.id}`));
  }
  pick.value = [...pick.options].some((o) => o.value === keep) ? keep : '/board';
  showBoard();
}

function showBoard() {
  const v = $('#board-pick').value;
  $('.history').classList.toggle('old', v !== '/board');
  $('#board').src = `${v}?t=${Date.now()}`;
}

async function load() {
  state = await api('/api/state');
  render();
  renderHistory();
}

async function showText(title, path, bank = false) {
  $('#dlg-h').textContent = title;
  $('#dlg-body').textContent = 'Loading…';
  $('#bank').hidden = !bank;
  $('#dlg').showModal();
  try { $('#dlg-body').textContent = (await api(path)).text; } catch (e) { $('#dlg-body').textContent = `Error: ${e.message}`; }
}

$('#report').addEventListener('submit', (e) => { e.preventDefault(); submit(true); });
$('#btn-save').addEventListener('click', () => submit(false));
$('#board-pick').addEventListener('change', showBoard);
$('#btn-totals').addEventListener('click', () => showText('Totals', '/api/totals'));
$('#btn-machines').addEventListener('click', () => showText('Machines', '/api/machines', true));
$('#dlg-close').addEventListener('click', () => $('#dlg').close());
$('#bank').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    $('#dlg-body').textContent = (await api('/api/bank', { kind: f.kind.value, item: f.item.value.trim(), qty: Number(f.qty.value) })).text;
    f.reset();
  } catch (err) { $('#dlg-body').textContent = `Error: ${err.message}`; }
});

load().catch((e) => setStatus(`Can't reach the desk server: ${e.message}`, true));
