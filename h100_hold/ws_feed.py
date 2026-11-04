#!/usr/bin/env python3
"""Lighter websocket feed for the H100 bot (Stockholm VPS only).

Keeps ONE persistent connection to wss://mainnet.zklighter.elliot.ai/stream,
subscribes to market_stats/182 and writes the latest index snapshot to
index_stream.json (atomic replace) in this directory. h100_bot.py reads that
file first and only falls back to REST when the snapshot is stale (>10 s),
so the bot sees the index live without polling — no rate-limit bans.

Run with the trading venv python (has `websockets`):
  /opt/trading/Trading-Intelligence/.venv/bin/python3 ws_feed.py
Stdlib-only fallback is intentionally NOT used: the venv is the runtime here.
"""
import asyncio, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_F = os.path.join(HERE, 'index_stream.json')
LOG_F = os.path.join(HERE, 'ws_feed.log')
URL = 'wss://mainnet.zklighter.elliot.ai/stream'
CHANNEL = 'market_stats/182'


def log(msg):
    line = '%s %s' % (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), msg)
    print(line, flush=True)
    try:
        with open(LOG_F, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def find_stats(obj):
    """Return the innermost dict carrying index_price, wherever it nests."""
    if isinstance(obj, dict):
        if 'index_price' in obj:
            return obj
        for v in obj.values():
            r = find_stats(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_stats(v)
            if r is not None:
                return r
    return None


def write_snapshot(stats):
    try:
        snap = {'ts': time.time(),
                'index': float(stats['index_price']),
                'mark': float(stats.get('mark_price') or stats['index_price']),
                'last': float(stats.get('last_trade_price') or stats['index_price']),
                'oi_usd': float(stats.get('open_interest_usd') or 0)}
    except Exception:
        return None
    tmp = OUT_F + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(snap, f)
    os.replace(tmp, OUT_F)
    return snap


async def run_once():
    import websockets
    async with websockets.connect(URL, ping_interval=20, ping_timeout=20,
                                  user_agent_header='h100-ws-feed/1.0') as ws:
        await ws.send(json.dumps({'type': 'subscribe', 'channel': CHANNEL}))
        log('connected + subscribed ' + CHANNEL)
        last_index = None
        raw_logged = 0
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if raw_logged < 3:
                log('raw frame: ' + raw[:300])
                raw_logged += 1
            stats = find_stats(msg)
            if stats is None:
                continue
            snap = write_snapshot(stats)
            if snap and snap['index'] != last_index:
                log('index %s -> %s' % (last_index, snap['index']))
                last_index = snap['index']


async def main():
    while True:
        try:
            await run_once()
        except Exception as e:
            log('connection error: %r — reconnect in 5 s' % (e,))
        await asyncio.sleep(5)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
