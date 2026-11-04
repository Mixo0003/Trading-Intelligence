#!/usr/bin/env python3
"""H100 HOLD — LIVE BOT, STANDALONE (owner order 2026-11-03: "це два різні
бота, бери переробляй" — the earlier mirror design, which followed the HOLD
paper engine's journal, was rejected by the owner the same day).

This bot has its OWN signal, copied 1:1 from the HOLD paper engine
(/opt/trading/h100-hold/h100_hold_bot.py) — same feed, same base point,
same formula:

  Feed: the Lighter H100 index (market 182). Primary source is the live
  websocket snapshot index_stream.json (written by ws_feed.py; the paper
  bot reads the very same file through a symlink), fresh <= 10 s; fallback
  is the Lighter REST orderBookDetails endpoint, index_price field.
  A step counts ONLY from the previous hour's boundary print: if the
  stored prev_index was not recorded inside the previous hour, the print
  is stale (a print was missed) — NO trade, prev is reseeded to the print
  actually seen (Hold's REALISM2 print-integrity rule, verbatim semantics).
  delta = index - prev_index;  delta_pct = delta / prev_index.
  delta_pct >= +7% -> LONG;  delta_pct <= -7% -> SHORT;  else no entry.
  (THRESHOLD_PCT = 0.07, owner 2026-10-31.)
  Hourly cycle (owner change 2026-11-04): a position closes 50 minutes
  after entry (TIME_CLOSE_50MIN — 10 minutes before the position's hour
  ends). The close at the next hour's print (HOURLY_CLOSE_BEFORE_NEW_HOUR)
  is kept ONLY as a fallback for a position that missed its 50-minute
  close. Hold has NO early percent exit.
  Startup adoption (owner 2026-11-04): in live mode the executor asks the
  exchange for its open H100-USDT-SWAP position at startup and ADOPTS it
  (side/qty/avgPx/notional/cTime) instead of refusing to start, so a
  position the executor did not open itself (restart, missed fill record)
  is still managed: profit-lock +$30 and the 50-minute close both apply.
  An adopted position older than 50 minutes closes at the first check.

The ONLY other exit is the owner's:

OWNER_RULE_PROFIT_LOCK_30 (owner, 2026-11-03, clarified twice):
  Entry is $100 margin x3. From the moment of entry, at ANY moment during
  the position's hour (even the first 5 minutes), when the position's net
  value — a walk over a FRESH OKX book minus the exit taker fee — reaches
  >= +$30.00, the position is closed in full (reduce-only, retried until
  flat, fill-verified). No new entries until that hour ends; the next hour
  runs the normal cycle. NOT a daily limit, NOT cumulative.

Other owner limits (standing):
  - Exposure cap: $300 total ($100 margin x 3, isolated).
  - Daily stop: executor realized PnL of the UTC day <= -$15 -> no new
    entries until the next UTC day (closes always continue).
  - KILL file /opt/trading/h100-bot/KILL_LIVE: while present, NO new
    entries; open positions are still closed reduce-only. Fail-closed:
    the executor ships with KILL in place.

Execution discipline (Anton's live lesson, owner requirement):
  - After EVERY order: verify the actual fill via order status + fills API.
    A partial fill is reality, not an error — the position is booked at the
    actually filled quantity.
  - Closes are reduce-only and REPEAT until the exchange position is
    exactly zero; every attempt uses a fresh book/market. After 5
    consecutive zero-fill attempts -> ALERT (live journal + ALERT_LIVE
    file) and retries continue every 60 s. Never silently give up.
  - After every action the executor position is reconciled against the
    exchange positions API; any mismatch is journaled.

Panel config (owner, 2026-11-03): the Telegram panel writes
/opt/trading/h100-bot/h100_live_config.json (stake_usd = margin,
profit_lock_usd, day_stop_usd SIGNED exactly as the owner typed it
(-50 = stop when the day reaches -50; +50 = stop at +50), leverage).
refresh_config() re-reads it at startup and before every new entry;
JSON values override the code constants below. A missing/broken JSON
falls back to the code constants and is journaled (CONFIG_FALLBACK).

Modes:
  dry-run (default): runs the bot's own signal, "executes" against the live
    OKX book with the fleet's own walk_fill + sampled order-flight delay,
    journals everything with simulated=true. Places NO orders. KILL state
    is journaled but does not stop the simulation (its purpose is to show
    what the bot WOULD do); in live mode KILL is a hard gate.
  live: requires OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE in
    /opt/trading/live-bot.env. Refuses to start without them.

Journal: /opt/trading/h100-bot/live_journal.jsonl (append-only).
State:   /opt/trading/h100-bot/live_state.json.
"""
import base64, hashlib, hmac, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

HERE = '/opt/trading/h100-bot'
sys.path.insert(0, HERE)
import h100_bot as hb   # fleet engine: public OKX helpers, walk_fill, delay model

LIVE_JOURNAL = HERE + '/live_journal.jsonl'
LIVE_STATE = HERE + '/live_state.json'
ALERT_F = HERE + '/ALERT_LIVE'
KILL_F = HERE + '/KILL_LIVE'
ENV_F = '/opt/trading/live-bot.env'

INST = hb.INST                      # H100-USDT-SWAP
MARGIN = 100.0                      # owner
LEVERAGE = 3                        # owner
NOTIONAL = MARGIN * LEVERAGE        # $300 exposure cap (owner)
TAKER_FEE = hb.TAKER_FEE            # 0.05% public Lv1 — verified 2026-11-02
PROFIT_LOCK_USD = 30.0              # OWNER_RULE_PROFIT_LOCK_30
DAY_STOP_USD = -15.0                # signed day stop: <0 loss stop, >0 profit stop
CLOSE_ALERT_AFTER = 5               # consecutive zero-fill close attempts -> ALERT
CLOSE_RETRY_S = 5                   # gap between immediate close retries
CLOSE_ALERT_RETRY_S = 60            # gap between retries after ALERT
POSITION_LIFETIME_S = 3000          # owner 2026-11-04: close 10 min before
                                    # the hour ends = 50 min after entry_ts
SAMPLE_S = 2.0                      # profit-lock sampling cadence
OKX_BASE = 'https://www.okx.com'

CONFIG_F = HERE + '/h100_live_config.json'   # written by the Telegram panel
# Code constants above are the FALLBACKS; the panel JSON wins when readable.
_CONFIG_DEFAULTS = {'margin': 100.0, 'leverage': 3, 'profit_lock_usd': 30.0, 'day_stop_usd': -15.0}
_config_seen = None                 # last raw config dict successfully applied

# ---------------------------------------------------------------- own signal
# Copied 1:1 from the HOLD paper engine (h100_hold_bot.py) — same feed, same
# base point, same formula. Kept as a local copy on purpose: the two bots are
# independent (owner, 2026-11-03); neither reads the other's files at runtime.
THRESHOLD_PCT = 0.07    # owner, 2026-10-31: entry from 7% (Hold's THRESHOLD_PCT)
LIGHTER_DETAILS = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails?market_id=182"
STREAM_F = HERE + '/index_stream.json'   # written by ws_feed.py (live Lighter
                                          # websocket); the paper bot reads the
                                          # same file via a symlink from its dir
INDEX_POLL_S = 1.0      # main-loop index poll cadence (the stream file is a
                        # local read; prints are caught within ~1 s)
_REST_MIN_INTERVAL = 5.0   # Lighter REST fallback: never poll faster than
                           # this (the paper fleet learned the hard way that
                           # back-to-back Lighter reads trip CloudFront
                           # throttling); the last REST value is reused
                           # between fetches
_REST_BACKOFF_MAX = 120.0  # Hold's LIGHTER_BACKOFF_MAX: on REST failures the
                           # gap between attempts grows 5s -> 10s -> 20s ->
                           # ... capped at 120 s; a success resets it
_rest_cache = {'ts': 0.0, 'val': None, 'fails': 0}


def _rest_min_interval():
    """Current REST retry gap: the base interval after a success, Hold's
    exponential backoff while failures accumulate."""
    f = _rest_cache['fails']
    if f <= 0:
        return _REST_MIN_INTERVAL
    return max(_REST_MIN_INTERVAL, min(_REST_BACKOFF_MAX, 5.0 * (2 ** min(f - 1, 5))))


def _get_json(url):
    # Hold's get() sends exactly this User-Agent ('h100-paper-bot/1.0'); the
    # header is kept identical so the access layer matches the paper bot's
    # 1:1. (Verified 2026-11-03: the hourly :59 405 bursts hit BOTH agents
    # identically — the endpoint refuses briefly at :59 regardless of UA;
    # both UAs also read 200/200/200/200 outside the burst.)
    req = urllib.request.Request(url, headers={'User-Agent': 'h100-paper-bot/1.0'})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def lighter_index():
    """The Lighter H100 index — Hold's lighter_index(), verbatim semantics:
    prefer the live websocket snapshot when it is fresh (<= 10 s old); else
    the REST orderBookDetails read (index_price). Access-layer additions,
    mirroring Hold's loop: the last REST value is reused between fetches,
    and failed fetches back off exponentially (Hold's LIGHTER_BACKOFF
    schedule) instead of retrying at poll cadence. A reused value equals
    the previous print and triggers nothing; a failed fetch raises and the
    caller simply skips that poll — the print value persists, so the cycle
    still fires on the first successful read, exactly as in Hold.
    Returns the index float; raises on total failure."""
    try:
        with open(STREAM_F) as f:
            s = json.load(f)
        if time.time() - float(s['ts']) <= 10 and s.get('index'):
            return float(s['index'])
    except Exception:
        pass
    now = time.time()
    if _rest_cache['val'] is not None and now - _rest_cache['ts'] < _rest_min_interval():
        return _rest_cache['val']
    try:
        d = _get_json(LIGHTER_DETAILS)['order_book_details'][0]
        val = float(d['index_price'])
    except Exception:
        _rest_cache['fails'] += 1
        _rest_cache['ts'] = now   # the backoff window runs from the failure
        raise
    _rest_cache.update(ts=now, val=val, fails=0)
    return val


def compute_signal(prev_index, index):
    """Hold's delta formula, verbatim: delta = index - prev;
    delta_pct = delta / prev; LONG at >= +THRESHOLD_PCT, SHORT at
    <= -THRESHOLD_PCT, else None. Returns (delta, delta_pct, signal)."""
    delta = index - prev_index
    delta_pct = delta / prev_index if prev_index else 0.0
    signal = 'LONG' if delta_pct >= THRESHOLD_PCT else ('SHORT' if delta_pct <= -THRESHOLD_PCT else None)
    return delta, delta_pct, signal


# ---------------------------------------------------------------- journal/state
def ljournal(rec):
    rec.setdefault('ts', int(time.time()))
    rec.setdefault('ts_utc', datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    with open(LIVE_JOURNAL, 'a') as f:
        f.write(json.dumps(rec) + '\n')


def alert(rec):
    rec.setdefault('ts', int(time.time()))
    rec.setdefault('ts_utc', datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    with open(ALERT_F, 'a') as f:
        f.write(json.dumps(rec) + '\n')
    jr = dict(rec)
    jr['alert_kind'] = jr.pop('type', None)
    jr['type'] = 'ALERT'
    ljournal(jr)


def load_state():
    try:
        with open(LIVE_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    tmp = LIVE_STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(s, f)
    os.replace(tmp, LIVE_STATE)


def refresh_config(source=''):
    """Re-read the panel config (h100_live_config.json).

    JSON values override the module constants MARGIN / LEVERAGE /
    PROFIT_LOCK_USD / DAY_STOP_USD (NOTIONAL is recomputed). The panel
    stores day_stop_usd SIGNED, exactly as the owner typed it: negative
    (-50) = entries stop when realized day PnL reaches that loss;
    positive (+50) = entries stop when realized day PnL reaches that
    profit. refresh_config takes the sign as-is (no normalization).
    Missing/broken/implausible JSON -> fall back to the code constants
    and journal CONFIG_FALLBACK.
    Returns True when the JSON was applied."""
    global MARGIN, LEVERAGE, NOTIONAL, PROFIT_LOCK_USD, DAY_STOP_USD, _config_seen
    try:
        with open(CONFIG_F) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError('config root is not a JSON object')

        def pick(*names):
            for n in names:
                if cfg.get(n) is not None:
                    return cfg[n]
            return None

        m = pick('stake_usd', 'MARGIN', 'margin')
        lev = pick('leverage', 'LEVERAGE')
        lock = pick('profit_lock_usd', 'PROFIT_LOCK_USD')
        stop = pick('day_stop_usd', 'DAY_STOP_USD')
        new_margin = float(m) if m is not None else MARGIN
        new_lev = int(lev) if lev is not None else LEVERAGE
        new_lock = float(lock) if lock is not None else PROFIT_LOCK_USD
        if stop is not None:
            new_stop = float(stop)   # SIGNED, exactly as the owner set it
        else:
            new_stop = DAY_STOP_USD
        if new_margin <= 0 or new_lev < 1 or new_lock <= 0 or new_stop == 0:
            raise ValueError(f'implausible values margin={new_margin} lev={new_lev} '
                             f'lock={new_lock} stop={new_stop}')
    except Exception as e:
        MARGIN = _CONFIG_DEFAULTS['margin']
        LEVERAGE = _CONFIG_DEFAULTS['leverage']
        PROFIT_LOCK_USD = _CONFIG_DEFAULTS['profit_lock_usd']
        DAY_STOP_USD = _CONFIG_DEFAULTS['day_stop_usd']
        NOTIONAL = MARGIN * LEVERAGE
        ljournal({'type': 'CONFIG_FALLBACK', 'source': source, 'error': str(e)[:200],
                  'margin': MARGIN, 'leverage': LEVERAGE, 'notional': NOTIONAL,
                  'profit_lock_usd': PROFIT_LOCK_USD, 'day_stop_usd': DAY_STOP_USD})
        return False
    changed = (new_margin, new_lev, new_lock, new_stop) != (MARGIN, LEVERAGE, PROFIT_LOCK_USD, DAY_STOP_USD)
    MARGIN, LEVERAGE, PROFIT_LOCK_USD, DAY_STOP_USD = new_margin, new_lev, new_lock, new_stop
    NOTIONAL = MARGIN * LEVERAGE
    if changed or _config_seen is None:
        ljournal({'type': 'CONFIG_LOADED', 'source': source, 'margin': MARGIN,
                  'leverage': LEVERAGE, 'notional': NOTIONAL,
                  'profit_lock_usd': PROFIT_LOCK_USD, 'day_stop_usd': DAY_STOP_USD})
    _config_seen = cfg
    return True


def env_key_presence():
    """Names only — values are never read into the journal or printed."""
    names = set()
    try:
        with open(ENV_F) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    names.add(line.split('=', 1)[0].strip())
    except Exception:
        pass
    return {k: (k in names) for k in ('OKX_API_KEY', 'OKX_API_SECRET', 'OKX_API_PASSPHRASE')}


def env_values():
    vals = {}
    with open(ENV_F) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                vals[k.strip()] = v.strip()
    return vals


# ---------------------------------------------------------------- OKX private
class OKXPrivate:
    """Signed OKX REST. Instantiated ONLY in live mode with keys present."""

    def __init__(self, key, secret, passphrase):
        self.key, self.secret, self.passphrase = key, secret, passphrase

    def _req(self, method, path, body=None):
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        body_s = json.dumps(body) if body is not None else ''
        msg = ts + method + path + body_s
        sign = base64.b64encode(hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()
        req = urllib.request.Request(OKX_BASE + path, method=method,
                                     data=body_s.encode() if body_s else None,
                                     headers={'OK-ACCESS-KEY': self.key, 'OK-ACCESS-SIGN': sign,
                                              'OK-ACCESS-TIMESTAMP': ts, 'OK-ACCESS-PASSPHRASE': self.passphrase,
                                              'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'})
        with urllib.request.urlopen(req, timeout=25) as r:
            out = json.load(r)
        if out.get('code') != '0':
            raise RuntimeError(f"OKX {method} {path}: {out.get('code')} {out.get('msg')}")
        return out['data']

    def set_leverage(self):
        return self._req('POST', '/api/v5/account/set-leverage',
                         {'instId': INST, 'lever': str(LEVERAGE), 'mgnMode': 'isolated'})

    def place_market(self, cl_ord_id, side, sz, reduce_only):
        body = {'instId': INST, 'tdMode': 'isolated', 'side': side, 'ordType': 'market',
                'sz': str(int(sz)), 'clOrdId': cl_ord_id}
        if reduce_only:
            body['reduceOnly'] = True
        d = self._req('POST', '/api/v5/trade/order', body)
        if d[0].get('sCode') != '0':
            raise RuntimeError(f"place order rejected: {d[0].get('sCode')} {d[0].get('sMsg')}")
        return d[0]['ordId']

    def get_order(self, ord_id):
        return self._req('GET', f'/api/v5/trade/order?instId={INST}&ordId={ord_id}')[0]

    def get_fills(self, ord_id):
        return self._req('GET', f'/api/v5/trade/fills?instId={INST}&ordId={ord_id}')

    def get_positions(self):
        """Raw exchange position rows for INST (including zero-qty rows)."""
        return self._req('GET', f'/api/v5/account/positions?instId={INST}')

    def get_position_qty(self):
        """Signed quantity of the exchange position: (side, qty) or (None, 0)."""
        for p in self.get_positions():
            q = float(p.get('pos') or 0)
            if q != 0:
                return p.get('posSide'), abs(q)
        return None, 0.0

    def get_position_detail(self):
        """The single nonzero exchange position, or None when flat.
        Returns {'side','qty','avg_px','notional_usd','c_time_ms','raw_pos_side'}.
        Raises on >1 nonzero rows: an ambiguous exchange state is never
        guessed — startup fails closed, exactly like the old refusal."""
        rows = [p for p in self.get_positions() if float(p.get('pos') or 0) != 0]
        if not rows:
            return None
        if len(rows) > 1:
            raise RuntimeError(f'exchange has {len(rows)} nonzero positions — '
                               'refusing to adopt an ambiguous state')
        p = rows[0]
        q = float(p['pos'])
        raw_side = p.get('posSide')
        if raw_side == 'long':
            side = 'LONG'
        elif raw_side == 'short':
            side = 'SHORT'
        else:  # net mode: OKX reports pos signed (positive = long)
            side = 'LONG' if q > 0 else 'SHORT'
        avg_px = float(p.get('avgPx') or 0)
        notional = float(p.get('notionalUsd') or 0) or abs(q) * avg_px
        if avg_px <= 0 or notional <= 0:
            raise RuntimeError('exchange position has no usable avgPx/notional — '
                               'refusing to adopt')
        return {'side': side, 'qty': abs(q), 'avg_px': avg_px,
                'notional_usd': notional, 'c_time_ms': p.get('cTime'),
                'raw_pos_side': raw_side}


# ---------------------------------------------------------------- brokers
class SimBroker:
    """Dry-run broker: fills walk a FRESH live book after the fleet's sampled
    order-flight delay — the same execution model as the paper fleet."""

    simulated = True

    def __init__(self):
        self.pos = None          # {'side','qty','entry_vwap','entry_usd','entry_fee','entry_ts'}

    def market_fill(self, is_buy, want_qty=None, budget_usd=None):
        asks, bids, buy_lmt, sell_lmt = hb.exec_book()
        if is_buy:
            return hb.walk_fill(asks, buy_lmt, True, want_qty=want_qty, budget_usd=budget_usd)
        return hb.walk_fill(bids, sell_lmt, False, want_qty=want_qty, budget_usd=budget_usd)

    def open(self, side):
        is_buy = side == 'LONG'
        qty, vwap, usd, band_cut = self.market_fill(is_buy, budget_usd=NOTIONAL)
        if qty <= 0:
            return None
        self.pos = {'side': side, 'qty': qty, 'entry_vwap': vwap, 'entry_usd': usd,
                    'entry_fee': usd * TAKER_FEE, 'entry_ts': int(time.time()), 'band_cut': band_cut}
        return dict(self.pos)

    def close_attempt(self, qty):
        """One reduce-only attempt for qty contracts. Returns filled qty."""
        is_buy = self.pos['side'] == 'SHORT'   # closing a short = buy back
        fqty, vwap, usd, band_cut = self.market_fill(is_buy, want_qty=qty)
        if fqty <= 0:
            return 0.0, 0.0, 0.0, band_cut
        return fqty, vwap, usd, band_cut

    def apply_close(self, fqty, vwap, usd):
        pos = self.pos
        share = fqty / pos['qty']
        entry_part = pos['entry_usd'] * share
        pnl = (usd - entry_part - pos['entry_fee'] * share - usd * TAKER_FEE) * 1.0
        if pos['side'] == 'SHORT':
            pnl = (entry_part - usd - pos['entry_fee'] * share - usd * TAKER_FEE)
        pos['qty'] -= fqty
        pos['entry_usd'] -= entry_part
        pos['entry_fee'] -= pos['entry_fee'] * share
        if pos['qty'] <= 1e-9:
            self.pos = None
        return pnl

    def position_qty(self):
        return (self.pos['side'], self.pos['qty']) if self.pos else (None, 0.0)


class LiveBroker:
    """Real OKX execution. Every order is fill-verified; the position of
    record is the EXCHANGE position, re-read after every action."""

    simulated = False

    def __init__(self, client):
        self.c = client
        self.n = 0
        self.pos = None
        self.c.set_leverage()
        det = self.c.get_position_detail()
        if det is not None:
            self._adopt_position(det, 'startup')

    def _adopt_position(self, det, adopted_from):
        """Adopt the exchange position as the executor's position of record.

        Shared by startup adoption and the post-open reconcile (owner
        2026-11-04, patch 2): profit-lock, the 50-minute close and the
        entry gate then manage it exactly like a self-opened position.
        entry_fee is unknown at adoption and booked as 0.0 (journaled)."""
        if det['c_time_ms']:
            entry_ts = int(int(det['c_time_ms']) // 1000)
            ts_fallback = False
        else:
            entry_ts = int(time.time())
            ts_fallback = True
        self.pos = {'side': det['side'], 'qty': det['qty'],
                    'entry_vwap': det['avg_px'],
                    'entry_usd': det['notional_usd'], 'entry_fee': 0.0,
                    'entry_ts': entry_ts, 'adopted': True,
                    'adopted_from': adopted_from}
        ljournal({'type': 'ADOPTED_POSITION', 'side': det['side'],
                  'qty': det['qty'], 'entry_vwap': det['avg_px'],
                  'entry_usd': round(det['notional_usd'], 2),
                  'entry_ts': entry_ts, 'entry_ts_fallback_now': ts_fallback,
                  'entry_fee_note': 'unknown at adoption, booked as 0.0',
                  'adopted_from': adopted_from,
                  'raw_pos_side': det['raw_pos_side']})

    def _cl(self):
        self.n += 1
        return f'h100x{int(time.time())}{self.n}'[-32:]

    def _verified_fill(self, ord_id, timeout_s=20):
        """Poll order status, then read fills. Returns (filled_qty, avg_px, fee)."""
        end = time.time() + timeout_s
        state = None
        while time.time() < end:
            o = self.c.get_order(ord_id)
            state = o['state']
            if state in ('filled', 'canceled', 'mmp_canceled'):
                break
            time.sleep(0.5)
        fills = self.c.get_fills(ord_id)
        qty = sum(float(f['fillSz']) for f in fills)
        usd = sum(float(f['fillSz']) * float(f['fillPx']) for f in fills)
        fee = sum(float(f['fee']) for f in fills)
        return qty, (usd / qty if qty else 0.0), fee, state

    def open(self, side):
        # Market order sized in contracts from the current ask/bid, budget $300.
        asks, bids, _, _ = hb.exec_book()
        ref = asks[0][0] if side == 'LONG' else bids[0][0]
        sz = int(NOTIONAL / ref)
        if sz < 1:
            return None
        ord_id = self.c.place_market(self._cl(), 'buy' if side == 'LONG' else 'sell', sz, False)
        qty, vwap, fee, state = self._verified_fill(ord_id)
        if qty <= 0:
            ljournal({'type': 'ORDER_NO_FILL', 'ord_id': ord_id, 'state': state, 'side': side})
            # Owner 2026-11-04, patch 2 (the 13:01 case): the order may be
            # filled on the exchange while our own fills query comes back
            # empty. Never conclude flat on our own query alone: ask the
            # exchange for its position of record right now. Only if the
            # exchange also says flat is this an honest no-entry.
            try:
                det = self.c.get_position_detail()
            except Exception as e:
                ljournal({'type': 'OPEN_RECONCILE_ERROR', 'ord_id': ord_id,
                          'side': side, 'error': str(e)})
                return None
            if det is not None:
                self._adopt_position(det, 'open_reconcile')
                return dict(self.pos)
            return None
        self.pos = {'side': side, 'qty': qty, 'entry_vwap': vwap, 'entry_usd': qty * vwap,
                    'entry_fee': abs(fee), 'entry_ts': int(time.time()), 'ord_id': ord_id}
        self._reconcile()
        return dict(self.pos)

    def close_attempt(self, qty):
        side = 'sell' if self.pos['side'] == 'LONG' else 'buy'
        ord_id = self.c.place_market(self._cl(), side, int(qty), True)
        fqty, vwap, fee, state = self._verified_fill(ord_id)
        return fqty, vwap, fqty * vwap, False

    def apply_close(self, fqty, vwap, usd):
        pos = self.pos
        share = fqty / pos['qty']
        entry_part = pos['entry_usd'] * share
        if pos['side'] == 'LONG':
            pnl = usd - entry_part - pos['entry_fee'] * share - usd * TAKER_FEE
        else:
            pnl = entry_part - usd - pos['entry_fee'] * share - usd * TAKER_FEE
        pos['qty'] -= fqty
        pos['entry_usd'] -= entry_part
        pos['entry_fee'] -= pos['entry_fee'] * share
        if pos['qty'] <= 1e-9:
            self.pos = None
        self._reconcile()
        return pnl

    def _reconcile(self):
        side, qty = self.c.get_position_qty()
        mine = self.pos['qty'] if self.pos else 0.0
        if abs(qty - mine) > 1e-9:
            ljournal({'type': 'RECONCILE_MISMATCH', 'exchange_qty': qty, 'exchange_side': side,
                      'executor_qty': mine})
        return qty

    def position_qty(self):
        return self.c.get_position_qty()


# ---------------------------------------------------------------- executor
class Executor:
    def __init__(self, broker, mode):
        refresh_config('executor_init')
        self.broker = broker
        self.mode = mode
        self.st = load_state()
        self.st.setdefault('prev_index', None)       # own signal: last recorded print
        self.st.setdefault('prev_index_ts', None)    # ...and when it was recorded
        self.st.setdefault('day', None)              # 'YYYY-MM-DD' UTC
        self.st.setdefault('day_pnl', 0.0)
        self.st.setdefault('day_stop_until', 0)
        self.st.setdefault('lock_until', 0)          # profit-lock: no entries before this ts
        self.lock_fired_for = None

    # ---- gates
    def kill_present(self):
        return os.path.exists(KILL_F)

    def entries_allowed(self, now):
        if self.mode == 'live' and self.kill_present():
            return False, 'KILL_LIVE'
        if now < self.st['day_stop_until']:
            return False, 'DAY_STOP'
        if now < self.st['lock_until']:
            return False, 'PROFIT_LOCK_30_HOUR'
        return True, None

    # ---- day accounting
    def roll_day(self, now):
        d = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')
        if self.st['day'] != d:
            self.st['day'] = d
            self.st['day_pnl'] = 0.0
            save_state(self.st)

    def book_realized(self, pnl, now):
        self.roll_day(now)
        self.st['day_pnl'] += pnl
        hit = (DAY_STOP_USD < 0 and self.st['day_pnl'] <= DAY_STOP_USD) or \
              (DAY_STOP_USD > 0 and self.st['day_pnl'] >= DAY_STOP_USD)
        if hit and self.st['day_stop_until'] < now:
            nxt = (int(now) // 86400 + 1) * 86400
            self.st['day_stop_until'] = nxt
            ljournal({'type': 'DAY_STOP_HIT', 'day_pnl': round(self.st['day_pnl'], 4),
                      'day_stop_usd': DAY_STOP_USD, 'stop_until': nxt})
        save_state(self.st)

    # ---- close-to-zero (Anton's lesson)
    def close_to_zero(self, reason):
        """Reduce-only attempts until the position is EXACTLY zero.
        Returns realized pnl of the whole close."""
        total_pnl = 0.0
        zero_streak = 0
        attempt = 0
        while True:
            side, qty = self.broker.position_qty()
            if qty <= 1e-9 or self.broker.pos is None:
                break
            attempt += 1
            fqty, vwap, usd, band_cut = self.broker.close_attempt(qty)
            if fqty > 0:
                pnl = self.broker.apply_close(fqty, vwap, usd)
                total_pnl += pnl
                zero_streak = 0
                ljournal({'type': 'CLOSE_FILL', 'reason': reason, 'attempt': attempt,
                          'filled_qty': fqty, 'vwap': vwap, 'pnl': round(pnl, 4),
                          'simulated': self.broker.simulated})
            else:
                zero_streak += 1
                ljournal({'type': 'CLOSE_NO_FILL', 'reason': reason, 'attempt': attempt,
                          'zero_streak': zero_streak, 'band_cut': band_cut,
                          'simulated': self.broker.simulated})
                if zero_streak == CLOSE_ALERT_AFTER:
                    alert({'type': 'CLOSE_STUCK', 'reason': reason, 'attempts': attempt,
                           'qty_open': qty, 'note': 'reduce-only close not filling; retries continue every 60 s'})
            if self.broker.pos is None:
                break
            time.sleep(CLOSE_RETRY_S if zero_streak < CLOSE_ALERT_AFTER else CLOSE_ALERT_RETRY_S)
        ljournal({'type': 'CLOSE_DONE', 'reason': reason, 'total_pnl': round(total_pnl, 4),
                  'simulated': self.broker.simulated})
        return total_pnl

    # ---- own signal: entries
    def try_open(self, side, now, ctx=None):
        """Open a position from the bot's own signal. Panel config is
        re-read first (panel settings win per entry), then the gates."""
        refresh_config('pre_entry')   # panel settings win, re-read per entry
        if self.broker.pos is not None:
            ljournal({'type': 'ENTRY_SKIP', 'why': 'already in position', 'side': side,
                      'signal_ctx': ctx, 'simulated': self.broker.simulated})
            return None
        ok, why = self.entries_allowed(now)
        if not ok:
            ljournal({'type': 'ENTRY_BLOCKED', 'gate': why, 'side': side,
                      'signal_ctx': ctx, 'simulated': self.broker.simulated})
            return None
        pos = self.broker.open(side)
        if pos is None:
            ljournal({'type': 'OPEN_NO_FILL', 'side': side, 'signal_ctx': ctx,
                      'simulated': self.broker.simulated})
            return None
        self.lock_fired_for = None
        ljournal({'type': 'OPEN', 'side': side, 'qty': pos['qty'], 'entry_vwap': pos['entry_vwap'],
                  'entry_usd': round(pos['entry_usd'], 2), 'band_cut': pos.get('band_cut'),
                  'signal_ctx': ctx, 'simulated': self.broker.simulated})
        return pos

    # ---- own signal: the hourly decision cycle (Hold's cycle(), decision part)
    def signal_cycle(self, index, now):
        """Run Hold's decision logic on a newly seen print `index` at `now`.

        State machine copied from h100_hold_bot.cycle():
          prev is None -> INIT_NO_PREV_INDEX (seed; the caller seeds too).
          prev_index_ts outside the previous hour -> STALE_PREV_NO_TRADE:
          no trade on a step that spans 2+ hours; reseed prev to the print
          actually seen.
          Otherwise: delta/delta_pct/signal from compute_signal(); the
          hourly cycle then runs — an open position is closed FIRST
          (HOURLY_CLOSE_BEFORE_NEW_HOUR), and the new print is evaluated
          from flat: a >= 7% signal opens a fresh position (unless the
          profit-lock hour gate is still running)."""
        self.roll_day(now)
        prev = self.st.get('prev_index')
        rec = {'type': 'SIGNAL_CYCLE', 'index': index, 'prev_index': prev,
               'simulated': self.broker.simulated}
        if prev is None:
            rec['decision'] = 'INIT_NO_PREV_INDEX'
            rec['delta'] = None
            ljournal(rec)
            self.st['prev_index'] = index
            self.st['prev_index_ts'] = now
            save_state(self.st)
            return rec
        cur_start = (now // 3600) * 3600
        pts = self.st.get('prev_index_ts')
        if pts is None or not (cur_start - 3600 <= pts < cur_start):
            rec['delta'] = round(index - prev, 4)
            rec['decision'] = 'STALE_PREV_NO_TRADE'
            rec['stale_prev'] = {'prev_index_ts': pts, 'cur_hour_start': cur_start}
            ljournal(rec)
            self.st['prev_index'] = index
            self.st['prev_index_ts'] = now
            save_state(self.st)
            return rec
        delta, delta_pct, signal = compute_signal(prev, index)
        rec['delta'] = round(delta, 4)
        rec['delta_pct'] = round(delta_pct * 100, 3)
        rec['signal'] = signal
        # Fallback only (owner 2026-11-04: primary close is TIME_CLOSE_50MIN):
        # if a position somehow survived past its 50-minute close, close it
        # BEFORE the new hour's print is judged (owner's old cycle).
        if self.broker.pos is not None:
            pnl = self.close_to_zero('HOURLY_CLOSE_BEFORE_NEW_HOUR')
            self.book_realized(pnl, now)
            rec['hourly_close_pnl'] = round(pnl, 4)
        if signal:
            ctx = {'index': index, 'prev_index': prev, 'delta_pct': rec['delta_pct']}
            pos = self.try_open(signal, now, ctx=ctx)
            rec['decision'] = 'OPEN' if pos else 'SIGNAL_NO_ENTRY'
        else:
            rec['decision'] = 'NO_SIGNAL_FLAT'
        ljournal(rec)
        self.st['prev_index'] = index
        self.st['prev_index_ts'] = now
        save_state(self.st)
        return rec

    # ---- OWNER_RULE_PROFIT_LOCK_30
    def sample_profit_lock(self):
        pos = self.broker.pos
        if pos is None:
            return
        now = time.time()
        hour_end = (pos['entry_ts'] // 3600 + 1) * 3600
        if now >= hour_end or self.lock_fired_for == pos['entry_ts']:
            return
        asks, bids = hb.okx_book()
        levels = bids if pos['side'] == 'LONG' else asks
        limit = hb.okx_price_limit()
        lim = limit[1] if pos['side'] == 'LONG' else limit[0]
        qty, vwap, usd, band_cut = hb.walk_fill(levels, lim, pos['side'] == 'SHORT', want_qty=pos['qty'])
        if qty <= 0:
            return
        # owner formula: fresh-book walk value minus the exit fee, vs entry cost
        net = usd - pos['entry_usd'] - usd * TAKER_FEE
        if pos['side'] == 'SHORT':
            net = pos['entry_usd'] - usd - usd * TAKER_FEE
        if net >= PROFIT_LOCK_USD:
            self.lock_fired_for = pos['entry_ts']
            self.st['lock_until'] = hour_end
            save_state(self.st)
            ljournal({'type': 'PROFIT_LOCK_30_TRIGGERED', 'net_at_trigger': round(net, 4),
                      'entry_ts': pos['entry_ts'], 'lock_until': hour_end,
                      'simulated': self.broker.simulated})
            pnl = self.close_to_zero('PROFIT_LOCK_30')
            self.book_realized(pnl, int(time.time()))

    # ---- owner 2026-11-04: close 10 min before the hour ends
    def check_time_close(self, now=None):
        """Close a position that reached POSITION_LIFETIME_S (50 min) after
        entry_ts, via the existing close_to_zero (reduce-only retries).
        An adopted position older than 50 min closes at the first check.
        Profit-lock (+$30) is untouched and runs independently until this
        close fires. Returns True when a close ran."""
        pos = self.broker.pos
        if pos is None:
            return False
        now = int(time.time()) if now is None else now
        if now < pos['entry_ts'] + POSITION_LIFETIME_S:
            return False
        pnl = self.close_to_zero('TIME_CLOSE_50MIN')
        self.book_realized(pnl, now)
        return True

    # ---- main loop: own signal — poll the Lighter index, judge new prints
    def run(self):
        ljournal({'type': 'EXECUTOR_START', 'mode': self.mode, 'simulated': self.broker.simulated,
                  'kill_live': self.kill_present(), 'signal': 'OWN_LIGHTER_H100_M182',
                  'threshold_pct': THRESHOLD_PCT * 100, 'prev_index': self.st.get('prev_index'),
                  'prev_index_ts': self.st.get('prev_index_ts'), 'profit_lock_30': True,
                  'day_stop_usd': DAY_STOP_USD, 'notional_cap': NOTIONAL})
        last_sample = 0.0
        last_poll = 0.0
        last_idx_error = 0.0
        while True:
            now = time.time()
            if now - last_poll >= INDEX_POLL_S:
                last_poll = now
                try:
                    idx = lighter_index()
                except Exception as e:
                    idx = None
                    if now - last_idx_error >= 60:
                        last_idx_error = now
                        ljournal({'type': 'INDEX_ERROR', 'error': str(e)[:200]})
                if idx is not None:
                    try:
                        if self.st.get('prev_index') is None:
                            self.signal_cycle(idx, int(now))   # seeds prev (INIT)
                        elif idx != self.st['prev_index']:
                            self.signal_cycle(idx, int(now))   # a new print: judge it
                    except Exception as e:
                        ljournal({'type': 'LOOP_ERROR', 'error': str(e)[:200]})
            if now - last_sample >= SAMPLE_S:
                last_sample = now
                try:
                    self.sample_profit_lock()
                except Exception as e:
                    ljournal({'type': 'SAMPLE_ERROR', 'error': str(e)[:200]})
                try:
                    self.check_time_close()
                except Exception as e:
                    ljournal({'type': 'TIME_CLOSE_ERROR', 'error': str(e)[:200]})
            time.sleep(0.25)


def main():
    refresh_config('startup')
    mode = 'dry-run'
    if '--mode' in sys.argv:
        mode = sys.argv[sys.argv.index('--mode') + 1]
    presence = env_key_presence()
    if mode == 'live':
        if not all(presence.values()):
            msg = {'type': 'STARTUP_REFUSED', 'mode': 'live', 'reason': 'OKX keys missing in live-bot.env',
                   'key_presence': presence}
            ljournal(msg)
            print('REFUSED: OKX keys missing:', presence)
            return 2
        vals = env_values()
        broker = LiveBroker(OKXPrivate(vals['OKX_API_KEY'], vals['OKX_API_SECRET'], vals['OKX_API_PASSPHRASE']))
    else:
        broker = SimBroker()
    ljournal({'type': 'STARTUP', 'mode': mode, 'key_presence': presence, 'kill_live': os.path.exists(KILL_F)})
    Executor(broker, mode).run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
