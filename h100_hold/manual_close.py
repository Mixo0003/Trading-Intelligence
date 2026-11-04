"""Instant manual close for H100-USDT-SWAP. Usage: python3 manual_close.py
Reads keys from /opt/trading/live-bot.env, closes any open position reduce-only,
prints fills VWAP and gross PnL vs the last OPEN record in live_journal.jsonl.
Does NOT stop/start the executor service: run it while the service is stopped,
or accept that the running executor will reconcile a zero position."""
import json, hmac, hashlib, base64, urllib.request, time, sys
from datetime import datetime, timezone
vals = {}
for line in open('/opt/trading/live-bot.env'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        vals[k.strip()] = v.strip()
BASE = 'https://www.okx.com'
INST = 'H100-USDT-SWAP'

def req(method, path, body=None):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    bs = json.dumps(body) if body is not None else ''
    sign = base64.b64encode(hmac.new(vals['OKX_API_SECRET'].encode(), (ts + method + path + bs).encode(), hashlib.sha256).digest()).decode()
    r = urllib.request.Request(BASE + path, method=method, data=bs.encode() if bs else None,
        headers={'OK-ACCESS-KEY': vals['OKX_API_KEY'], 'OK-ACCESS-SIGN': sign,
                 'OK-ACCESS-TIMESTAMP': ts, 'OK-ACCESS-PASSPHRASE': vals['OKX_API_PASSPHRASE'],
                 'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'})
    out = json.load(urllib.request.urlopen(r, timeout=25))
    if out.get('code') != '0':
        raise RuntimeError(f"{method} {path}: {out.get('code')} {out.get('msg')}")
    return out['data']

def main():
    pos = req('GET', f'/api/v5/account/positions?instId={INST}')
    openpos = [p for p in pos if float(p.get('pos') or 0) != 0]
    if not openpos:
        print('NO_POSITION')
        return 0
    p = openpos[0]
    qty = int(abs(float(p['pos'])))
    side = p['posSide']
    close_side = 'buy' if side == 'short' else 'sell'
    oid = req('POST', '/api/v5/trade/order', {'instId': INST, 'tdMode': 'isolated', 'side': close_side,
        'ordType': 'market', 'sz': str(qty), 'reduceOnly': True,
        'clOrdId': f"manual-close-{int(time.time())}"})[0]['ordId']
    print('ORDER', oid)
    for _ in range(15):
        time.sleep(2)
        o = req('GET', f'/api/v5/trade/order?instId={INST}&ordId={oid}')[0]
        if o.get('state') != 'live':
            break
    fills = req('GET', f'/api/v5/trade/fills?instId={INST}&ordId={oid}&limit=100')
    fq = sum(float(f['fillSz']) for f in fills)
    fv = sum(float(f['fillPx']) * float(f['fillSz']) for f in fills) / fq if fq else 0
    print('FILL_VWAP', round(fv, 4), 'FILL_QTY', fq)
    pos2 = req('GET', f'/api/v5/account/positions?instId={INST}')
    print('POS_AFTER', json.dumps([q for q in pos2 if float(q.get('pos') or 0) != 0]))
    entry = None
    try:
        for line in open('/opt/trading/h100-bot/live_journal.jsonl'):
            r = json.loads(line)
            if r.get('type') == 'OPEN':
                entry = (r['side'], r['entry_vwap'])
    except FileNotFoundError:
        pass
    if entry and entry[0] == side:
        gross = (entry[1] - fv) * fq if side == 'short' else (fv - entry[1]) * fq
        print('GROSS_PNL_USD', round(gross, 2))
    return 0

if __name__ == '__main__':
    sys.exit(main())
