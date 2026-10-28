// localStorage persistence: settings and the flip log.
const SETTINGS_KEY = 'rsf:settings:v1';
const FLIPS_KEY = 'rsf:flips:v1';

export const DEFAULT_SETTINGS = {
  budget: 50_000_000,   // cash you are willing to tie up in ONE item
  taxPct: 2,            // RS3 GE tax (wiki: 2 % since 9 Jan 2023). Editable.
  minPrice: 1000,
  minVolume: 1000,      // daily items traded
  capturePct: 5,        // share of daily volume you can expect to fill yourself
  candidates: 60,       // how many items get a 90-day history check
  bandK: 0.5,           // spread width = bandK × daily volatility
  dipZ: 1.5,            // dip = price this many std-devs under the 30-day mean
  members: true,
  notify: false,
};

export function loadSettings() {
  try {
    const s = JSON.parse(localStorage.getItem(SETTINGS_KEY));
    if (s && typeof s === 'object') {
      const out = { ...DEFAULT_SETTINGS };
      for (const k of Object.keys(DEFAULT_SETTINGS)) {
        if (typeof s[k] === typeof DEFAULT_SETTINGS[k]) out[k] = s[k];
      }
      return out;
    }
  } catch { /* fall through */ }
  return { ...DEFAULT_SETTINGS };
}
export const saveSettings = (s) => localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));

export function loadFlips() {
  try {
    const f = JSON.parse(localStorage.getItem(FLIPS_KEY));
    return Array.isArray(f) ? f : [];
  } catch { return []; }
}
export const saveFlips = (f) => localStorage.setItem(FLIPS_KEY, JSON.stringify(f));
