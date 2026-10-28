# RS3 Flip Finder

A static web page that suggests Grand Exchange flips for RuneScape 3. It shows buy and sell prices, profit after tax, and a quantity that the trade volume can actually support. It also logs your flips.

## Run it

```bash
cd C:/Programming/Repos/rs3-flipper
python -m http.server 8765 --bind 127.0.0.1
```
Then open http://127.0.0.1:8765. It must be served over HTTP: ES modules don't load from `file://`. There's no install and no build step.

Leave the tab open. Every 10 minutes it checks whether Jagex has published a new GE update. When one arrives it re-ranks the list and, if you turn this on, sends a desktop notification with the top 3 flips.

## Data sources
| What | Where | Why |
|---|---|---|
| Guide price, daily volume, buy limit, item ID, members flag for every item | `runescape.wiki/w/Module:GE{Prices,Volumes,Limits,IDs,Members}/data.json?action=raw` | One request per field. Sends CORS headers. |
| 90-day price and volume history | `api.weirdgloop.org/exchange/history/rs/last90d?id=…` | The wiki's own GE history API. Sends CORS headers. |
| Update check | `api.weirdgloop.org/exchange/history/rs/latest?id=314` | Small poll. |

The official `secure.runescape.com/m=itemdb_rs/api` serves the same numbers, but it sends no CORS headers, so a web page can't call it without a backend proxy.

## What the numbers mean
- **The web page** still works from the daily guide price, so its buy and sell prices are **estimates**. Check them with **Check margin**. **The trade desk** (below) uses the wiki's live price feed instead (`prices.runescape.wiki/api/v2/rs`, Jagex data refreshed every 60 s), so its buy and sell prices come from real instant-buy and instant-sell trades.
- **Spread flip:** buy at guide × (1 − h) and sell at guide × (1 + h). Here h = *spread width* × the 30-day daily volatility, kept between 0.3 % and 8 %.
- **Dip recovery:** the guide price is at least *dip threshold* standard deviations below the 30-day mean. Buy at the guide and sell near the mean. This takes days.
- **Quantity** is the smallest of: the 4-hour buy limit, cash per item ÷ buy price, and (your share % × daily volume) ÷ 6. The daily volume used is the lower of today's and the 30-day median.
- **Tax:** `floor(sell × tax%)` per item, paid by the seller. Sales under 50 gp and Bonds pay none. The default is 2 %, the rate the wiki gives (in force since 9 Jan 2023). You can change it in settings.
- **Confidence** drops for these flags: falling, spiking, volume spike, price frozen, thin, volatile, still dropping.

## Trade desk (twice-daily plans)
`tools/desk.py` plans up to 8 GE offers against a ledger in `ledger/state.json`. The ledger tracks cash, the capital cap, open offers, holdings and realised P/L. Prices come from the live feed: buy at the instant-sell price + 1, sell at the instant-buy price − 1. A 7-day hourly history checks that the margin holds. Strategies are flips, dips, and **processing** (buy whirligig shells, grind them, sell the powder). A watchlist (golden roses, prayer powders and shells, every bone and ash) is always analysed: `desk.py watch`.

```bash
python tools/desk.py plan            # next plan (saved to ledger/plans/)
python tools/desk.py status
python tools/desk.py fill 2 --to 3000 --price 2065
python tools/desk.py cancel 3
python tools/desk.py set --capital 200m
```
A Hermes cron job runs `plan` at 07:00 and 18:00 local and posts the plan to chat. More commands: `totals`, `plan --away DAYS`, `plan --review-all`, `edit N --pin`, `block add NAME`, `limit NAME --resets HH:MM`, `watch`. Run `python tools/desk.py -h` for the full list.

### Data and backups
`ledger/` holds your trading data: `state.json` plus rendered plans and per-write backups. It is **not** in git (see `.gitignore`). After each scheduled plan, `tools/backup.py` copies it to `OneDrive/Backup-anwar/rs3-flipper/`. That copy keeps one snapshot a day for 90 days, so it survives a dead disk. Restore: copy a `state-YYYY-MM-DD.json` back to `ledger/state.json`.

### Tests
```bash
python -m unittest discover -s tests -v
```
These cover the money logic (tax, break-even, quantity rounding, fills, buy-limit windows, grinding, pins, blocklist, track record, away analysis). They use a temp ledger and fake prices, so no network is needed and the real ledger is never touched. Run them before changing `tools/`.

## Files
- `tools/desk.py`: trade desk CLI (planner + ledger)
- `tools/rsmarket.py`: live price data, tax, statistics, strategies
- `tools/plan_widget.html`: template for the in-chat plan panel
- `tools/backup.py`: off-folder ledger backup
- `tests/test_desk.py`: unit tests
- `index.html`: markup (Basecoat 1.0.2 + Tailwind v4 browser build from jsDelivr)
- `css/app.css`
- `js/api.js`: all network access and caching
- `js/analysis.js`: tax, statistics and suggestions (pure functions)
- `js/store.js`: settings and the flip log in localStorage
- `js/app.js`: UI

The Tailwind browser build generates its CSS at runtime. That's fine for a personal tool, but Tailwind doesn't recommend it for production sites.
