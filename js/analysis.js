// Pure maths for the flipping suggestions. No DOM, no network.
//
// Data reality for RS3 (unlike OSRS): Jagex publishes ONE guide price and ONE
// daily volume per item per GE update (~once a day). There is no public feed of
// live buy/sell offers, so every buy/sell price here is an ESTIMATE derived from
// the guide price and its recent volatility. Confirm with an in-game margin check.

/** GE tax per item. Wiki (Grand Exchange#Trade tariff): seller pays a % of the
 *  sale price per item, rounded down; sales under 50 coins and Bonds are exempt. */
export function geTax(sell, rate, name = '') {
  if (name === 'Bond' || sell < 50) return 0;
  return Math.floor(sell * rate);
}

export const netSell = (sell, rate, name) => sell - geTax(sell, rate, name);

/** Lowest sell price that still makes at least 1 gp per item after tax. */
export function breakEvenSell(buy, rate, name) {
  let s = Math.max(buy + 1, Math.floor(buy / (1 - rate)) - 2);
  while (netSell(s, rate, name) <= buy) s++;
  return s;
}

const avg = (a) => (a.length ? a.reduce((s, x) => s + x, 0) / a.length : 0);
function stdev(a) {
  if (a.length < 2) return 0;
  const m = avg(a);
  return Math.sqrt(a.reduce((s, x) => s + (x - m) ** 2, 0) / (a.length - 1));
}
function median(a) {
  if (!a.length) return 0;
  const s = [...a].sort((x, y) => x - y);
  const i = s.length >> 1;
  return s.length % 2 ? s[i] : (s[i - 1] + s[i]) / 2;
}
const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));

/** Stats from weirdgloop last90d points: [{price, volume, timestamp}] */
export function historyStats(points) {
  const pts = points.filter((p) => p.price > 0).sort((a, b) => a.timestamp - b.timestamp);
  const n = pts.length;
  if (n < 10) return null;
  const last30 = pts.slice(-30);
  const prices = last30.map((p) => p.price);
  const rets = [];
  for (let i = 1; i < last30.length; i++) rets.push(Math.log(last30[i].price / last30[i - 1].price));
  const vols = last30.map((p) => p.volume).filter((v) => v != null);
  const cur = pts[n - 1].price;
  const flatDays = (() => { // consecutive identical prices at the end = no real trading
    let k = 0;
    for (let i = n - 1; i > 0 && pts[i].price === pts[i - 1].price; i--) k++;
    return k;
  })();
  return {
    mean30: avg(prices),
    sd30: stdev(prices),
    min30: Math.min(...prices),
    max30: Math.max(...prices),
    sigma: stdev(rets),               // daily log-return volatility
    medVol30: median(vols),
    ch1: n > 1 ? cur / pts[n - 2].price - 1 : 0,
    ch7: n > 7 ? cur / pts[n - 8].price - 1 : 0,
    ch30: cur / last30[0].price - 1,
    flatDays,
    spark: prices,
  };
}

/**
 * Build suggestions for one item.
 * item: {id, name, price, volume, limit, members}
 * cfg:  {budget, taxRate, capture, bandK, dipZ}
 * Returns [] or [{strategy, buy, sell, profit, qty, total, roi, hours, flags, confidence, score}]
 */
export function suggest(item, st, cfg) {
  if (!st) return [];
  const p = item.price;
  // Be conservative: use the lower of today's volume and the 30-day median.
  const dailyVol = Math.min(item.volume || 0, st.medVol30 || item.volume || 0);
  if (dailyVol <= 0) return [];
  const yourShare = dailyVol * cfg.capture;             // items/day you can realistically fill
  const limit = item.limit > 0 ? item.limit : Infinity; // buy limit per 4 hours
  const qtyFor = (buy) => Math.floor(Math.min(limit, yourShare / 6, cfg.budget / buy));
  const hoursFor = (qty) => (qty / yourShare) * 24;     // one side (buy OR sell)

  const flags = [];
  if (st.ch7 < -0.1) flags.push('falling');
  if (st.ch7 > 0.15) flags.push('spiking');
  if (item.volume > 3 * (st.medVol30 || Infinity)) flags.push('volume spike');
  if (st.flatDays >= 5) flags.push('price frozen');
  if (dailyVol < 100) flags.push('thin');
  if (st.sigma > 0.05) flags.push('volatile');

  const out = [];
  const push = (strategy, buy, sell, extraFlags = []) => {
    if (!(sell > buy) || buy < 1) return;
    const profit = netSell(sell, cfg.taxRate, item.name) - buy;
    const qty = qtyFor(buy);
    if (profit <= 0 || qty < 1) return;
    const fl = [...flags, ...extraFlags];
    let confidence = 1;
    for (const f of fl) confidence *= ['thin', 'price frozen', 'volatile'].includes(f) ? 0.4 : 0.6;
    const hours = strategy === 'spread' ? 2 * hoursFor(qty) : null;
    out.push({
      strategy, buy, sell, profit, qty,
      total: profit * qty,
      roi: profit / buy,
      hours,
      flags: fl,
      confidence,
      score: profit * qty * confidence,
    });
  };

  // A) Spread flip: offer to buy a bit under the guide and sell a bit over it.
  //    Band = bandK × daily volatility, clamped to 0.3 %..8 %.
  const h = clamp(st.sigma * cfg.bandK, 0.003, 0.08);
  push('spread', Math.floor(p * (1 - h)), Math.ceil(p * (1 + h)));

  // B) Dip recovery: guide price is well under its 30-day mean; target the mean.
  const z = st.sd30 > 0 ? (p - st.mean30) / st.sd30 : 0;
  if (z <= -cfg.dipZ && p < st.mean30 * 0.97) {
    const sell = Math.floor(st.mean30 - 0.25 * st.sd30);
    push('dip', p, sell, st.ch1 < -0.03 ? ['still dropping'] : []);
  }
  return out;
}

/** Parse "50m", "1.5b", "250k", "12,000" → number (NaN if invalid). */
export function parseGp(s) {
  const m = String(s).trim().toLowerCase().replace(/[, _]/g, '').match(/^(\d+(?:\.\d+)?)([kmb]?)$/);
  if (!m) return NaN;
  return Math.round(Number(m[1]) * { '': 1, k: 1e3, m: 1e6, b: 1e9 }[m[2]]);
}
