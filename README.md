# TradeBot: stock + crypto auto-trader with a phone dashboard

A bot that runs on a small cloud server and trades:

- **Crypto** on **Upbit**: KRW pairs, around the clock
- **Korean stocks** (KOSPI/KOSDAQ) and **US stocks** through **Korea Investment & Securities (KIS Developers)**

You watch and control it from the **TradeBot Android app** (`android/`) on your phone. The trading itself runs on the server, so it keeps going when your phone is off.

```
 every minute ─► prices (Upbit / KIS) ─► rules (EMA trend + RSI, or breakout)
                                          │ BUY?
                                          ▼
              filters: trend strength (ADX) · long-term trend · volume
                                          ▼
              market regime: is BTC / KOSPI 200 / S&P 500 in an uptrend?
                                          ▼
                          risk check (size, daily loss, max positions)
                                          ▼
                    Claude review: news & red flags ─► approve / veto
                                          ▼
                     order (paper simulation  or  real broker)
                                          ▼
              SQLite log ─► dashboard on your phone (pause · kill switch)
```

> **Read this first.** No strategy or AI can predict markets. This bot carries out rules you choose and limits losses. It does not guarantee profits. Run it in **paper mode for several weeks** before using real money, and only use money you can afford to lose. I'm not a financial advisor, and nothing here is investment advice.

---

## 1. Try it right now (no keys, fake prices)

```bash
pip install -r requirements.txt
TRADEBOT_CONFIG=config.demo.toml DASHBOARD_TOKEN=demo123 \
  uvicorn app.server:create_app --factory --port 8000
```

Open http://localhost:8000 and sign in with `demo123`. Two-step login is off in this quick demo. In demo mode, synthetic prices move fast (one candle every 10 seconds), so trades show up within minutes.

Run the tests with `pytest -q`.

## 2. How it decides

| Step | What happens |
|---|---|
| **Rules** (`app/strategies.py`) | `trend_rsi` buys when the 20-EMA is above the 50-EMA and either a fresh crossover or an RSI bounce back above 40 fires, as long as RSI is below 70. It sells when the trend flips or RSI goes above 80. `vol_breakout` buys a break above open + 0.5 × yesterday's range and sells at the next candle. |
| **Filters** | `trend_rsi` also needs a strong trend (ADX ≥ 20), price above its 200-candle average, and volume at least 1.2× normal on the signal candle. `vol_breakout` needs the candle's volume on pace for 1.5× normal. Blocked setups appear as **filtered** in the Signals tab, so you can see what the filters are doing. |
| **Dip-buy strategy** | `dip_buy` buys a sharp short-term drop (2-period RSI under 10) while price is still above its 200-candle average. It sells on the bounce back above the 5-candle average, or after 10 candles. It's meant for choppy markets where trend rules struggle. |
| **Order guards** | No new stock buys in the first 15 minutes after the open. No buys when the bid-ask spread is over 0.3% or the last price is more than 5 minutes old. Exchange holidays are respected (US by rule, KRX from the KIS calendar). KRX orders use 최유리지정가 so they never sweep the order book. |
| **Correlation limit** | Symbols that move together count as one bet. By default BTC and ETH share one slot, as do Samsung and SK Hynix. |
| **Market regime** | No buys unless the wider market is in an uptrend: KRW-BTC for crypto, KODEX 200 for Korean stocks, SPY for US stocks, each closing above its 50-day average. |
| **Stops** | Each buy starts with a stop 2 × ATR below entry. Once the price is up by that same distance (+1R), the stop moves to break-even. After that it trails 3 × ATR below the highest price. There's no fixed take-profit, so winners can keep running. The bot checks stops every minute. |
| **Risk** (`app/risk.py`) | Each position is capped at 10% of equity. A trade can lose at most 1% of equity if its stop is hit. At most 5 positions and 10 buys per day. New buys stop for the day after a 3% drawdown. **Sells are never blocked.** |
| **Claude** (`app/ai_review.py`) | Claude looks at each buy and checks recent news. It can only **approve or veto**. It never starts a trade or changes the size. If Claude can't be reached, the buy is skipped. |

All limits are in `config.toml`.

## 3. Set up the real accounts

**Upbit (crypto)**
1. On upbit.com, go to 마이페이지 → Open API 관리 → Open API Key 발급.
2. Turn on 자산조회, 주문조회 and 주문하기 only. **Never turn on 출금 (withdrawals).**
3. Add your server's **fixed IP** to the whitelist. This is why the bot runs on a server and not on your phone.

**KIS (stocks)**
1. Open a 한국투자증권 account and apply for **KIS Developers** at apiportal.koreainvestment.com.
2. Apply for a **모의투자 (mock trading)** account and its app key/secret. Start with `KIS_ENV=demo`.
3. To trade US stocks, enable overseas trading. The bot assumes 통합증거금 (integrated margin), so KRW cash can fund USD buys.

**Claude**: create an API key at console.anthropic.com. Each buy review typically costs a few cents, and reviews only run when a rule fires.

Then:
```bash
cp .env.example .env    # fill in keys + a long DASHBOARD_TOKEN
nano config.toml        # watchlist, risk limits
```

## 4. Deploy to a server (small VPS, roughly $5–10/month)

Any small Linux VPS works, for example Vultr Seoul, AWS Lightsail Seoul or Oracle Cloud's free tier. It needs a **static IP** for Upbit's whitelist.

```bash
# on the server
git clone <your repo> tradebot && cd tradebot
cp .env.example .env && nano .env      # keys, token, DOMAIN=bot.yourdomain.com
docker compose up -d --build
docker compose logs -f bot
```

Point your domain's DNS A-record at the server. Caddy then gets an HTTPS certificate on its own.

**On your Android phone:** install the TradeBot app (below) and enter `bot.yourdomain.com` when it asks for your server. Opening the address in Chrome also works.

### The Android app

`android/` is a small native app (Android 10+, no third-party libraries). It opens your bot's dashboard full-screen and adds:

- a first-run screen that checks the server address before saving it
- a fingerprint / screen-lock prompt when the app opens (on by default, toggle under Settings → Android app; re-locks after a minute in the background)
- tax CSV downloads saved to your phone's Downloads folder
- links to other sites open in your normal browser, and a bad HTTPS certificate is always refused

**Getting the APK.** Push this project to a private GitHub repo. The workflow in `.github/workflows/ci.yml` tests the code, builds the app on GitHub's servers and publishes it as a release named **Android app**. On your phone, open the repo's **Releases**, download `TradeBot.apk`, open it, and allow your browser to install unknown apps when Android asks. You only do this once (see automatic updates below).

### Automatic updates

Once set up, you never redownload anything:

1. A change is pushed to `main`. GitHub runs the tests.
2. **Only if they pass**, the `release` branch moves to that commit and a new app build is published.
3. Every 5 minutes your server (`deploy/update.sh`, run by a systemd timer) pulls `release`, rebuilds the bot if anything changed, and copies the newest app to `data/app/`. `.env`, `data/` and your database are never touched. Settings you change in the dashboard live in the database, so they survive updates; hand edits to `config.toml` do not, so make changes through the dashboard or in git.
4. The phone app checks your server when it opens (and from **Settings → Android app → Check for updates**), downloads the update in the background, checks its SHA-256, and installs it the next time you leave the app. The very first self-update asks you to tap **Update** once; after that Android lets it update silently (Android 12+). Android itself rejects any update not signed with the app's key.

The dashboard itself is served by the bot, so dashboard changes appear as soon as the server updates.

**One-time server setup.** Create a GitHub **fine-grained token** with access to only this repo and **Contents: Read-only**. Then on the server:

```bash
read -rsp "GitHub token: " T; echo; printf '%s' "$T" > /root/.tradebot-github-token; chmod 600 /root/.tradebot-github-token
git -c http.extraHeader="Authorization: Basic $(printf 'x-access-token:%s' "$T" | base64 -w0)" clone https://github.com/<you>/tradebot /root/tradebot-git
bash /root/tradebot-git/deploy/install-updater.sh
```

Check what the updater did with `journalctl -u tradebot-update -n 30`.

To build it yourself instead: open `android/` in Android Studio, or run `./gradlew assembleRelease` with the Android SDK installed.

## 5. Alerts, health checks and safety monitors

**Phone alerts** (Telegram, KakaoTalk or both; keys go in `.env`):
- **Telegram:** message @BotFather → `/newbot` → put the token in `TELEGRAM_BOT_TOKEN`. Send your new bot any message, then run `python -m app.alerts telegram-chat-id`.
- **KakaoTalk (나에게 보내기):** create an app at developers.kakao.com, put its REST key in `KAKAO_REST_API_KEY`, then run `python -m app.alerts kakao-setup` once.
- Check it works: `python -m app.alerts test`, or **Settings → Send test alert**.

You'll get a message for every buy and sell, when a stop moves to break-even, on errors, failed orders, the daily loss limit, auto-pauses, new logins and failed logins. A summary also arrives every evening at 21:00 KST.

**Is the bot alive?**
- **Dead-man switch:** create a free check at healthchecks.io and put its ping URL in `HEALTHCHECK_URL`. The bot pings it every minute, and if the pings stop, healthchecks.io messages you.
- **`/healthz`:** returns 200 while the bot is checking markets and 503 if it has stalled. Point any uptime monitor at it.

**Automatic safety checks**
- **Auto-pause** stops new buys if equity falls 10% from its peak, or if the last 20 trades' profit factor drops below 0.8. Exits keep running, and you resume from the dashboard.
- **Position check (live mode):** every day at 10:00 KST the bot compares what it thinks it holds with what Upbit and KIS actually hold, and alerts you on any mismatch.
- **Monthly re-test:** on the 1st of each month the walk-forward test re-runs on fresh data for every symbol. You get a pass/fail report. Results also show in **Insights** and as badges on the watchlist.

**Insights tab, "Are the filters earning their keep?"** Every setup blocked by Claude, the market filter or a strategy filter is followed as if it had been taken, with the same stops. After a month or two you'll see, in R, whether each one saved you money or cost you gains. If Claude's vetoes aren't helping, turn the review off in Settings and save the API cost.

## 6. Market conditions, long-term holdings and more

**Market-condition filters** (`[signals]` in config.toml) can only block or shrink new buys. If a data source is down, that filter steps aside instead of freezing the bot. Every block is followed in shadow mode, so **Insights** shows whether each filter is helping.

| Filter | What it watches | Effect |
|---|---|---|
| VIX | The US "fear gauge" (FRED daily data, Cboe fallback) | Above 30, or up 20% in a day: stock positions are **halved** (or blocked) |
| Economic calendar | Fed (FOMC) rate decisions, CPI inflation and jobs-report days (built-in dates through 2027) | No buys that US day. Korean stocks: no buys the next morning, when Korea reacts |
| Bitcoin ETF flows | Daily US spot Bitcoin ETF flows | Crypto buys blocked after more than $500M of net outflows over 3 sessions |
| Funding rate | BTC perpetual-futures funding (Binance, Bybit fallback) | Crypto buys blocked when longs are crowded (over 0.05% per 8h) |
| Coinbase premium | Coinbase vs Binance Bitcoin price | Crypto buys blocked when US investors are selling (premium below −0.15%) |
| Upbit premium | Upbit vs global Bitcoin price | Upbit buys blocked when Korea is overheated (more than 5% above global) |

ETF flow data comes from *The Commoner Bitcoin ETF Tracker by TFTC* (tftc.io/bitcoin-etf-flows), licensed CC BY 4.0. The dashboard shows this credit. The built-in event dates run through 2027; after that, add dates under `macro_extra`.

**More risk controls**
- **Total risk cap:** if every open stop were hit at once, you'd lose at most 5% of the account. New buys shrink or wait to fit.
- **Weekend crypto:** from Saturday 00:00 to Monday 09:00 KST, crypto positions are half size.
- **Momentum ranking** (`[ranking]`, off by default): only the 3 strongest symbols by 3-month return may be bought.
- **Dollar exposure:** the Overview shows how much of the account is in USD, and how much the won/dollar rate has added or cost since you bought.

**Long-term holdings** have no stops and are managed separately from swing trades. Pause stops their buys too, and the kill switch sells them too.
- **Core, dual momentum** (`[core]`, off by default, needs KIS US trading): once a month, hold SPY or VEU (whichever was stronger over 12 months) while US stocks beat cash, otherwise AGG bonds. It uses `pct` of the account; swing trades size off the rest. Test it first: `python -m app.backtest --dual-momentum --bars 2000`.
- **Smart DCA** (`[[dca]]`): a fixed amount daily, weekly or monthly. It doubles when RSI is under 30 and adds 50% when the price is below its 200-day average, up to 3× in total.
- **Grid** (`[[grid]]`, Upbit only): for a coin moving sideways. It buys a step each time the price moves down and sells it one step up, and sells everything if the price breaks below the range. Test a range first: `python -m app.backtest --symbol KRW-XRP --interval 60m --bars 2000 --grid 2700 3300 10`.

**Side-by-side paper accounts** (`[[variants]]`) run alternative settings on the same live prices, with no real orders and no alerts. Two are included: *no-claude* (Claude review off) and *tighter-stops*. **Insights** compares their swing-trade P&L, win rate and drawdown with the main bot. A few weeks of this tells you more than more backtests.

**Ask Claude** (Insights tab): ask "Why did you buy NVDA?" or "Which filter has saved me the most?". Claude answers from the bot's own records. It can't trade or change settings, and you get up to 40 questions a day.

## 7. Safety net

**Real-time stops.** Crypto positions are watched on Upbit's live price feed, so a stop reacts within seconds, day or night. Stock positions are checked every 10 seconds while the market is open. The once-a-minute loop still handles everything else.

**Unfilled US orders.** If a US limit order hasn't filled after 20 seconds, the bot re-prices it 0.5% further, up to 2 times, then cancels whatever is left. You get an alert either way. (`[execution] us_retries`, `us_step_pct`)

**Guardian: stops that survive a dead server.** Your stops live in the bot, so if the server dies they stop working. The guardian is a small program for a *second* machine (your old Windows PC, a second cheap VPS or a Raspberry Pi):

1. On the bot server, set `GUARDIAN_TOKEN=<long random string>` in `.env` and restart.
2. On the second machine, copy the `tradebot` folder, `pip install requests PyJWT python-dotenv`, and create a `.env` with the same `GUARDIAN_TOKEN` plus your Telegram/Kakao alert keys.
3. Optional, so it can sell on its own: create a **separate Upbit API key** whitelisted to the second machine's IP (trading permission only, never withdrawals), and add your KIS keys.
4. Run `python -m app.guardian --url https://bot.yourdomain.com`. On Windows, Task Scheduler can start it at login.

It copies your positions and stops every 30 seconds. If the bot can't be reached for 5 minutes, you get an alert and the guardian starts watching prices itself. When a stop is hit, it sells (if it has keys) or sends a "SELL NOW" alert. When the bot comes back, it tells you what happened. Insights shows when the guardian last checked in.

*Why not stop orders at the broker?* Upbit has stop orders (예약주문) in its app, but I couldn't confirm the API can create them, and KIS doesn't offer true stop orders through its API. The guardian covers both brokers.

**Nightly backups** run at 03:00 KST. They're encrypted if `TRADEBOT_PASSPHRASE` is set, the last 14 are kept, and they're sent to your Telegram chat if that's set up. They can also go anywhere with `BACKUP_COMMAND` (for example `rclone copy … gdrive:TradeBot`). To back up immediately, use Settings → Back up now. To restore, stop the bot and run `python -m app.backup restore <file>`.

**Small-live start.** When you first go live, set `live_capital_krw = 500000` (or whatever you're comfortable losing). The bot then behaves as if the account held only that much. A **paper twin** with identical settings runs alongside automatically, and Insights compares them. If live does much worse than the twin, slippage or fills are hurting you.

**Stress tests and Monte Carlo**
```bash
python -m app.backtest --symbol KRW-BTC --market upbit --interval day --bars 3500 --stress
python -m app.backtest --symbol 005930 --market kis_kr --bars 1800 --stress
python -m app.backtest --symbol KRW-BTC --market upbit --interval 60m --bars 3000 --walkforward --montecarlo
```
- `--stress` replays the 2018 crypto winter, the COVID crash, the 2022 stock bear market and the 2022 crypto crash, whichever fall inside the loaded history.
- `--montecarlo` re-orders the trades 5,000 times to show realistic worst-case drops and losing streaks.
- Insights shows the same analysis for your live or paper trades once you have 10 or more closed trades.

## 8. Strategy options

- **Inverse ETFs (profit in falling markets):** add one with `inverse = true`, for example KODEX 인버스 (114800) or SH. It's only bought when the market filter says the market is *falling*, and it's sold as soon as the market turns back up. VIX caution doesn't block it, since fear helps an inverse position.
- **Partial profits** (`partial_r`, off by default): sell part of a winner at, say, +2R and let the rest ride the trailing stop.
- **Adding to winners** (`pyramid_r`, off by default): add 50% once a trade is up +1R *and* its stop is at break-even, once per trade, within the total-risk cap.
- **`auto` strategy:** uses the trend rules when the market is trending (ADX ≥ 20) and dip-buying when it's choppy. Each trade exits by the rules that opened it.
- **Allocation by performance** (`[allocation]`, off by default): symbols that fail the monthly re-test get half risk (or none), and strong ones get 25% more.

All of these work in backtests and walk-forward, so test them there before switching them on.

**More filters:**
- **Korean stocks:** foreigners and institutions must have been net buyers over the last 5 sessions (KIS investor data).
- **US stocks:** no buys within 2 days of earnings. This needs a free `FINNHUB_API_KEY`; without one the filter steps aside.
- **Crypto:** no buys when the Fear & Greed Index is 80+ (extreme greed). Data from Alternative.me, credited in the app.

## 9. Conveniences

- **Claude's weekly review:** every Sunday at 20:00 KST, a short written review of the week (what worked, which filters help, risks, settings worth testing) arrives as an alert and in Insights. "Write one now" makes one on demand.
- **Idle cash parking** (`[parking]`, off by default): spare KIS cash is parked in KODEX CD금리액티브 (459580), a money-market ETF that earns roughly the CD rate, during KRX hours. A buffer (20% of equity) stays in cash for US buys at night. One share costs about ₩1.07M.
- **Year-end tax review:** Settings shows realized gains so far this year and open positions at a loss. On December 1 you get an alert listing positions you could sell before December 31 to offset gains. Check with your tax advisor first: US wash-sale rules can apply if you buy the same security back within 30 days.

## 10. Using the dashboard

- **Overview:** equity, drawdown from peak, positions with their current stop, watchlist with market-trend arrows and test badges, pause, and the kill switch.
- **Journal:** each closed trade with why it was bought and sold. Tap one to see a price chart with the buy and sell points.
- **Signals:** every setup and what happened to it (executed, filtered, blocked, vetoed).
- **Insights:** shadow results, average slippage, recent profit factor, position check and re-test results.
- **Settings:** change risk limits, the market filter and Claude review; turn symbols on or off, add or remove them (a symbol with an open position can't be removed); send a test alert; download the **tax CSV**; see and sign out devices. Changes are saved in the database and survive restarts. **Reset** goes back to `config.toml`. Paper/live mode can only be changed on the server, never from the phone.

**Tax export:** one row per closed trade with acquisition and sale dates, cost basis, proceeds and gain in KRW and USD. It uses the ECB USD/KRW rate recorded on each trade date, which is the format US filers need for worldwide gains. Have a tax professional review it.

## 11. Security

1. **Two-step login:** run `python -m app.security totp-setup`, add the key to Google Authenticator or Authy, and put `TOTP_SECRET` in `.env`. Signing in then needs your password plus the 6-digit code.
2. **Per-device sessions:** each phone or browser that signs in gets its own 30-day session. **Settings → Signed-in devices** shows them, and you can sign any of them out. After 5 wrong attempts from one address, that address is locked out for 15 minutes, and you're alerted after 3.
3. **Encrypted keys on the server:** `python -m app.vault encrypt .env` creates `secrets.enc`. Delete `.env`, then start with `TRADEBOT_PASSPHRASE` set (for example `TRADEBOT_PASSPHRASE=... docker compose up -d`, typed in, not saved in a file). Anyone who copies the disk gets only encrypted data.
4. **API keys:** Upbit keys with no withdrawal permission and an IP whitelist; KIS keys kept to this server. Change `DASHBOARD_TOKEN` if you ever get failed-login alerts you don't recognise.

## 12. Testing a strategy: walk-forward

```bash
python -m app.backtest --symbol KRW-BTC --market upbit --interval 60m --bars 3000 --walkforward --regime
python -m app.backtest --symbol 005930  --market kis_kr --bars 1000 --walkforward --regime
python -m app.backtest --symbol KRW-ETH --market upbit --interval 60m --bars 3000 --walkforward --strategy vol_breakout
```

To test the whole watchlist together, with shared cash, the position limit and correlation groups:

```bash
python -m app.backtest --portfolio --bars 750 --regime
```

A walk-forward test tries every combination of settings on one stretch of history. It then scores the winner on the **next** stretch, which it has never seen, and repeats this 4 times. Only those unseen results count. The report gives you:

- **Out-of-sample return** compared with *holding the same-size position*. The bot only puts 10% into any one symbol, so comparing it with holding 100% wouldn't be fair.
- **Profit factor, win rate and worst drawdown** on the unseen data.
- **Settings stability**: how often each period picked the same settings. Low stability means a fragile edge.
- **A plain-language verdict.** It recommends settings to paste into `config.toml` (`params = {...}`) only when the unseen results beat holding with a profit factor above 1.1 and enough trades to judge.

## 13. Paper → live checklist

1. Run the walk-forward test (with `--montecarlo`) and `--stress` on every symbol you want to trade. Drop symbols that fail.
2. Run `trading_mode = "paper"` with `data_source = "live"` for **at least 4 weeks**.
3. Compare paper results with the walk-forward results. If paper is much worse, something is off. Don't go live.
4. Optional middle step: keep paper mode but set `KIS_ENV=demo` with live data to confirm the KIS connection works.
5. Set up the guardian on a second machine and check that a nightly backup arrives.
6. Go live small: `trading_mode = "live"` with `live_capital_krw` set to an amount you're willing to lose. Compare live with the paper twin for a month before raising it.
7. Check the dashboard daily. **Pause** stops new buys, while exits keep running. **Kill switch** sells everything at market.

## 14. Known limits

- **Not tested against the live Upbit/KIS servers.** Endpoints follow the official docs and KIS sample code, but run paper mode with live data first to confirm every call works on your accounts.
- US early-close days (1pm closes) aren't modelled, so an order near the close on those days may be rejected.
- KIS price quotes don't include a trade time, so the stale-price guard only works for Upbit. KIS staleness is caught by the candle-date check.
- The portfolio backtest uses daily candles for every symbol so they line up in time.
- The market-condition filters aren't in backtests (most of their data only exists live). Shadow mode measures them going forward.
- The Upbit premium uses the daily ECB USD/KRW rate, so it's approximate to within the day's currency move.
- Bitcoin ETF flows arrive once a day after the US close, so the filter reacts to yesterday's flows.
- Grids use market orders when the price crosses a step (checked every minute), so fills can be a little past the step price.
- The guardian protects swing positions only. Long-term holdings (core, DCA, grid, parking) have no stops by design.
- Stock real-time checks poll every 10 seconds instead of using KIS's live feed, which needs a separate approval-key setup.
- The KIS unfilled-order list works only on real accounts. On the mock server, fills are detected from your balance.
- KRX earnings dates aren't covered; the earnings filter is US only.
- KIS fills are confirmed by re-reading your balance. If a US limit order doesn't fill within 20 seconds, the bot logs a warning and doesn't track it. Check the KIS app if you see that warning.
- Market holidays aren't modelled. Orders sent on a holiday are rejected and logged.
- In backtests, the `vol_breakout` volume filter sees the whole breakout candle's volume. Live, it only sees volume so far, so backtests of that strategy are slightly optimistic.
- Long history requests take a while. KIS mock accounts allow about 2 calls per second, and 1,000 daily candles take around 10 calls.
- The bot only manages positions it opened itself. It doesn't touch other holdings in your accounts.
- In live mode, the dashboard cash figure is Upbit KRW plus the KIS KRW deposit. USD cash isn't counted.

## Files

```
app/strategies.py  rules (trend, breakout, dip)   app/risk.py       position sizing & limits
app/engine.py      the trading loop               app/ai_review.py  Claude veto
app/shadow.py      what blocked trades would do   app/monitor.py    auto-pause, summary, checks, re-test
app/market.py      prices, regime, order guards   app/calendars.py  exchange holidays
app/brokers/       Upbit + KIS API                app/execution.py  paper vs live orders
app/alerts.py      Telegram / KakaoTalk / ping    app/security.py   2-step login, sessions
app/vault.py       encrypted secrets              app/tax.py        tax CSV        app/fx.py  USD/KRW
app/signals.py     market-condition filters        app/longterm.py   core, DCA, grid
app/variants.py    side-by-side paper accounts     app/ask.py        Ask Claude + weekly review
app/realtime.py    real-time stop checks           app/guardian.py   backup stop-keeper (2nd machine)
app/backup.py      nightly backups / restore
app/backtest.py    backtest, walk-forward, portfolio, dual momentum, grid
app/server.py      API + dashboard server         dashboard/        phone UI       config.toml  settings
android/           the Android app (Java)         .github/workflows/ci.yml  tests, release branch, APK
deploy/            server auto-updater
```
