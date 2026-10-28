// The only module that talks to the network.
//
// Sources (both send `Access-Control-Allow-Origin: *`, so they work from a static page;
// the official secure.runescape.com API does NOT, which is why it isn't used directly):
//  - RuneScape Wiki bulk modules, refreshed by the wiki's GE bot after every Jagex update:
//      https://runescape.wiki/w/Module:GE{Prices,Volumes,Limits,IDs,Members}/data.json?action=raw
//  - Weird Gloop exchange API (same data, with history):
//      https://api.weirdgloop.org/exchange/history/rs/last90d?id=ID   (1 item per call)
//      https://api.weirdgloop.org/exchange/history/rs/latest?id=ID    (up to 100 ids)
// Please be polite: history is cached per GE update in localStorage, and the update
// check polls one tiny endpoint every 10 minutes.

const WIKI = 'https://runescape.wiki/w/Module:';
const WG = 'https://api.weirdgloop.org/exchange/history/rs';
const MODULES = ['Prices', 'Volumes', 'Limits', 'IDs', 'Members'];
const HIST_PREFIX = 'rsf:hist:';

async function getJson(url) {
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${url}`);
  return res.json();
}

/** All tradeable items with guide price, daily volume, 4h buy limit, members flag. */
export async function loadMarket() {
  const [prices, volumes, limits, ids, members] = await Promise.all(
    MODULES.map((m) => getJson(`${WIKI}GE${m}/data.json?action=raw`)),
  );
  const items = [];
  for (const [name, price] of Object.entries(prices)) {
    if (name.startsWith('%') || typeof price !== 'number') continue;
    const id = ids[name];
    if (!id) continue;
    items.push({
      id, name, price,
      volume: volumes[name] ?? 0,
      limit: limits[name] ?? 0,
      members: members[name] !== false,
    });
  }
  return {
    items,
    updated: prices['%LAST_UPDATE%'] * 1000, // ms epoch of the Jagex GE update
  };
}

/** 90 days of daily [price, volume] for one item, cached for the given GE update. */
export async function loadHistory(id, updateStamp) {
  const key = HIST_PREFIX + id;
  try {
    const hit = JSON.parse(localStorage.getItem(key));
    if (hit && hit.stamp === updateStamp) return hit.points;
  } catch { /* corrupt entry: refetch */ }
  const data = await getJson(`${WG}/last90d?id=${id}`);
  const points = (data[id] || []).map((p) => ({ price: p.price, volume: p.volume, timestamp: p.timestamp }));
  try {
    localStorage.setItem(key, JSON.stringify({ stamp: updateStamp, points }));
  } catch { pruneHistory(Infinity); } // quota hit: drop the cache, carry on
  return points;
}

/** Remove cached histories that belong to an older GE update. */
export function pruneHistory(currentStamp) {
  for (let i = localStorage.length - 1; i >= 0; i--) {
    const k = localStorage.key(i);
    if (!k?.startsWith(HIST_PREFIX)) continue;
    try {
      if (JSON.parse(localStorage.getItem(k)).stamp !== currentStamp) localStorage.removeItem(k);
    } catch { localStorage.removeItem(k); }
  }
}

/** Timestamp (ms) of the newest GE update known to the API — cheap poll. */
export async function latestUpdateStamp() {
  const d = await getJson(`${WG}/latest?id=314`); // Feather: traded every single update
  return Date.parse(d['314'].timestamp);
}

/** Run async fn over items with limited parallelism; reports progress. */
export async function pool(items, size, fn, onProgress) {
  const results = new Array(items.length);
  let next = 0, done = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      try { results[i] = await fn(items[i]); } catch (e) { results[i] = e; }
      onProgress?.(++done, items.length);
    }
  };
  await Promise.all(Array.from({ length: Math.min(size, items.length) }, worker));
  return results;
}
