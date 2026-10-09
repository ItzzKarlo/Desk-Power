#!/usr/bin/env python3
"""Desk Power: tiny multi-Tapo P115 monitor with safe individual outlet control."""
import asyncio
import hmac
import logging
import os
import re
import sqlite3
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from dotenv import load_dotenv
from kasa import Discover, Module

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
LOG = logging.getLogger('desk-power')

TAPO_USERNAME = os.getenv('TAPO_USERNAME', '').strip()
TAPO_PASSWORD = os.getenv('TAPO_PASSWORD', '')
DASH_USER = os.getenv('DASH_USER', 'admin')
DASH_PASSWORD = os.getenv('DASH_PASSWORD', '')
HTTP_HOST = os.getenv('HTTP_HOST', '0.0.0.0')
HTTP_PORT = int(os.getenv('HTTP_PORT', '8766'))
POLL_SECONDS = max(5, int(os.getenv('POLL_SECONDS', '10')))
OFF_MAX_WATTS = max(0.0, float(os.getenv('OFF_MAX_WATTS', '30')))
EUR_PER_KWH = max(0.0, float(os.getenv('EUR_PER_KWH', '0.30')))
DB_PATH = Path(os.getenv('DB_PATH', str(ROOT / 'data' / 'history.sqlite')))


def configured_plugs():
    """TAPO_PLUGS=zeus@10.10.0.50,monitors@10.10.0.51.

    IDs remain stable if an IP changes, preserving each plug's history.
    """
    raw = os.getenv('TAPO_PLUGS', '').strip()
    if not raw and os.getenv('TAPO_IP'):
        # Backwards-compatible one-plug configuration
        raw = f"desk@{os.environ['TAPO_IP'].strip()}"
    result = {}
    for entry in filter(None, (x.strip() for x in raw.split(','))):
        if '@' not in entry:
            raise ValueError(f'Invalid TAPO_PLUGS entry {entry!r}: expected id@ip')
        plug_id, address = (part.strip() for part in entry.split('@', 1))
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', plug_id):
            raise ValueError(f'Invalid plug ID: {plug_id!r} (use lowercase letters, digits, - or _)')
        if not address or any(c in address for c in '/?#:@ \\'):
            raise ValueError(f'Invalid host for {plug_id!r}')
        if plug_id in result or address in result.values():
            raise ValueError(f'Duplicate plug ID or IP: {entry!r}')
        result[plug_id] = address
    return result


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        # Keep any legacy v1 "samples" table intact; it has no plug identity
        self.db.execute('CREATE TABLE IF NOT EXISTS plug_samples '
                        '(plug_id TEXT NOT NULL, ts INTEGER NOT NULL, watts REAL NOT NULL, '
                        'on_state INTEGER NOT NULL, PRIMARY KEY (plug_id, ts))')
        self.db.execute('CREATE INDEX IF NOT EXISTS idx_plug_samples_ts ON plug_samples(ts)')
        self.db.commit()
        self.last_saved = dict(self.db.execute(
            'SELECT plug_id, MAX(ts) FROM plug_samples GROUP BY plug_id').fetchall())

    def save(self, plug_id, watts, on):
        now = int(time.time())
        if now - self.last_saved.get(plug_id, 0) < 60:
            return
        self.db.execute('INSERT OR REPLACE INTO plug_samples VALUES (?, ?, ?, ?)',
                        (plug_id, now, float(watts), int(on)))
        self.db.commit()
        self.last_saved[plug_id] = now

    def history(self, period, selected, ids):
        windows = {'1h': (3600, 60), '24h': (86400, 300), '7d': (604800, 1800), '30d': (2592000, 7200)}
        duration, bucket = windows[period]
        since = int(time.time()) - duration
        if selected == 'total':
            # Only show a combined point when EVERY configured plug has a sample
            # in that bucket. Partial data must never be represented as total usage.
            rows = self.db.execute(
                'SELECT ts_bucket * ? AS ts, ROUND(SUM(avg_watts), 2) AS watts '
                'FROM (SELECT (ts / ?) AS ts_bucket, plug_id, AVG(watts) AS avg_watts '
                'FROM plug_samples WHERE ts >= ? GROUP BY ts_bucket, plug_id) '
                'WHERE plug_id IN ({}) GROUP BY ts_bucket HAVING COUNT(*) = ? ORDER BY ts'.format(
                    ','.join('?' for _ in ids)),
                (bucket, bucket, since, *ids, len(ids))
            ).fetchall()
        else:
            rows = self.db.execute(
                'SELECT (ts / ?) * ? AS ts, ROUND(AVG(watts), 2) AS watts '
                'FROM plug_samples WHERE ts >= ? AND plug_id = ? GROUP BY (ts / ?) ORDER BY ts',
                (bucket, bucket, since, selected, bucket)
            ).fetchall()
        return {'range': period, 'plug': selected, 'bucket_seconds': bucket,
                'points': [{'ts': ts, 'watts': watts} for ts, watts in rows]}

    def prune(self):
        self.db.execute('DELETE FROM plug_samples WHERE ts < ?', (int(time.time()) - 90 * 86400,))
        self.db.commit()

    def close(self):
        self.db.close()


class Plug:
    def __init__(self, plug_id, address, store):
        self.id = plug_id
        self.address = address
        self.store = store
        self.device = None
        self.lock = asyncio.Lock()
        self.online = False
        self.is_on = None
        self.watts = None
        self.today_kwh = None
        self.month_kwh = None
        self.alias = plug_id.replace('-', ' ').replace('_', ' ').title()
        self.updated_at = None
        self.error = 'Connecting to plug'

    def snapshot(self):
        return {
            'id': self.id, 'name': self.alias, 'online': self.online,
            'on': self.is_on if self.online else None,
            'watts': self.watts if self.online else None,
            'today_kwh': self.today_kwh if self.online else None,
            'month_kwh': self.month_kwh if self.online else None,
            'updated_at': self.updated_at, 'error': self.error,
            'off_max_watts': OFF_MAX_WATTS,
        }

    async def _connect(self):
        if self.device is None:
            self.device = await Discover.discover_single(
                self.address, username=TAPO_USERNAME, password=TAPO_PASSWORD)
            if self.device is None:
                raise ConnectionError(f'Tapo {self.id} not found')

    async def _update_locked(self):
        await self._connect()
        await self.device.update()
        energy = self.device.modules.get(Module.Energy)
        if energy is None:
            raise RuntimeError('Energy measurements unavailable')
        self.is_on = bool(self.device.is_on)
        self.watts = energy.current_consumption
        self.today_kwh = energy.consumption_today
        self.month_kwh = energy.consumption_this_month
        self.alias = self.device.alias or self.alias
        self.updated_at = int(time.time())
        self.online = True
        self.error = None
        if self.watts is not None:
            self.store.save(self.id, self.watts, self.is_on)

    async def _reset_after_error(self, error):
        LOG.warning('P115 %s at %s: %s', self.id, self.address, error)
        self.online = False
        self.error = 'Plug unavailable. Check its Wi-Fi, IP and Tapo credentials'
        if self.device is not None:
            with suppress(Exception):
                await self.device.disconnect()
        self.device = None

    async def refresh(self):
        async with self.lock:
            try:
                await asyncio.wait_for(self._update_locked(), timeout=15)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._reset_after_error(error)

    async def set_power(self, turn_on):
        async with self.lock:
            try:
                await asyncio.wait_for(self._update_locked(), timeout=15)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._reset_after_error(error)
                raise web.HTTPServiceUnavailable(text='Plug is unreachable') from error

            if not turn_on:
                if self.watts is None:
                    raise web.HTTPConflict(text='No live wattage. OFF refused for safety')
                if self.watts > OFF_MAX_WATTS:
                    raise web.HTTPConflict(text=f'OFF blocked: {self.alias} uses {self.watts:.1f} W '
                                                f'(limit {OFF_MAX_WATTS:g} W). Shut the computer down first')
            try:
                await asyncio.wait_for(self.device.turn_on() if turn_on else self.device.turn_off(), timeout=12)
                await asyncio.wait_for(self._update_locked(), timeout=15)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._reset_after_error(error)
                raise web.HTTPServiceUnavailable(text='Switch failed; check Tapo app before retrying') from error
            LOG.info('Plug %s switched %s', self.id, 'ON' if turn_on else 'OFF')
            return self.snapshot()

    async def close(self):
        if self.device is not None:
            with suppress(Exception):
                await self.device.disconnect()


def total_snapshot(plugs):
    snapshots = [p.snapshot() for p in plugs.values()]
    complete = bool(snapshots) and all(p['online'] for p in snapshots)
    def total(metric):
        if not complete or any(p[metric] is None for p in snapshots):
            return None
        return round(sum(p[metric] for p in snapshots), 4)
    return {
        'online': complete,
        'online_count': sum(bool(p['online']) for p in snapshots),
        'plug_count': len(snapshots),
        'watts': total('watts'),
        'today_kwh': total('today_kwh'),
        'month_kwh': total('month_kwh'),
        'eur_per_kwh': EUR_PER_KWH,
        'plugs': snapshots,
    }


@web.middleware
async def security(request, handler):
    import base64
    auth = request.headers.get('Authorization', '')
    authorized = False
    if auth.startswith('Basic '):
        try:
            decoded = base64.b64decode(auth[6:], validate=True).decode('utf-8')
            username, password = decoded.split(':', 1)
            authorized = hmac.compare_digest(username, DASH_USER) and hmac.compare_digest(password, DASH_PASSWORD)
        except (ValueError, UnicodeDecodeError):
            pass
    if not authorized:
        raise web.HTTPUnauthorized(headers={'WWW-Authenticate': 'Basic realm="Desk Power"'})
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        if request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType(text='JSON required')
        origin = request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != request.host:
            raise web.HTTPForbidden(text='Cross-origin switching is disabled')
    return await handler(request)


async def get_state(request):
    response = web.json_response(total_snapshot(request.app['plugs']))
    response.headers['Cache-Control'] = 'no-store'
    return response


async def get_history(request):
    period = request.query.get('range', '24h')
    selected = request.query.get('plug', 'total')
    plugs = request.app['plugs']
    if period not in ('1h', '24h', '7d', '30d'):
        raise web.HTTPBadRequest(text='Supported ranges: 1h, 24h, 7d, 30d')
    if selected != 'total' and selected not in plugs:
        raise web.HTTPBadRequest(text='Unknown plug')
    response = web.json_response(request.app['store'].history(period, selected, list(plugs)))
    response.headers['Cache-Control'] = 'no-store'
    return response


async def set_power(request):
    plug_id = request.match_info['plug_id']
    plug = request.app['plugs'].get(plug_id)
    if plug is None:
        raise web.HTTPNotFound(text='Unknown plug')
    try:
        body = await request.json()
    except (ValueError, web.HTTPException):
        raise web.HTTPBadRequest(text='Expected JSON like {"on": true}')
    if not isinstance(body, dict) or type(body.get('on')) is not bool:
        raise web.HTTPBadRequest(text='Expected JSON like {"on": true}')
    return web.json_response(await plug.set_power(body['on']))


async def index(request):
    return web.FileResponse(ROOT / 'static' / 'index.html', headers={'Cache-Control': 'no-cache'})


async def background_poll(app):
    app['poll_task'] = asyncio.create_task(poll_forever(app['plugs'], app['store']))


async def poll_forever(plugs, store):
    last_prune = 0
    while True:
        await asyncio.gather(*(plug.refresh() for plug in plugs.values()))
        if time.monotonic() - last_prune > 86400:
            store.prune()
            last_prune = time.monotonic()
        await asyncio.sleep(POLL_SECONDS)


async def shutdown(app):
    app['poll_task'].cancel()
    with suppress(asyncio.CancelledError):
        await app['poll_task']
    await asyncio.gather(*(plug.close() for plug in app['plugs'].values()))
    app['store'].close()


def make_app():
    addresses = configured_plugs()
    if not addresses:
        raise ValueError('Set TAPO_PLUGS in .env')
    store = Store(DB_PATH)
    app = web.Application(middlewares=[security], client_max_size=4096)
    app['store'] = store
    app['plugs'] = {name: Plug(name, address, store) for name, address in addresses.items()}
    app.router.add_get('/', index)
    app.router.add_get('/api/state', get_state)
    app.router.add_get('/api/history', get_history)
    app.router.add_post('/api/plugs/{plug_id}/power', set_power)
    app.router.add_static('/', ROOT / 'static', show_index=False)
    app.on_startup.append(background_poll)
    app.on_cleanup.append(shutdown)
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if not all((configured_plugs(), TAPO_USERNAME, TAPO_PASSWORD, DASH_USER, DASH_PASSWORD)):
        raise SystemExit('Configure TAPO_PLUGS, TAPO_USERNAME, TAPO_PASSWORD, DASH_USER, DASH_PASSWORD in .env')
    LOG.info('Serving Desk Power for %s P115(s) on http://%s:%s',
             len(configured_plugs()), HTTP_HOST, HTTP_PORT)
    web.run_app(make_app(), host=HTTP_HOST, port=HTTP_PORT, access_log=None)
