#!/usr/bin/env python3
"""Desk Power: tiny local Tapo P115 monitor + switch UI (no Home Assistant)."""
import asyncio
import hmac
import logging
import os
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

TAPO_IP = os.getenv('TAPO_IP', '').strip()
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


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, watts REAL NOT NULL, on_state INTEGER NOT NULL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)')
        self.db.commit()
        self.last_saved = 0

    def save(self, watts, on):
        now = int(time.time())
        if now - self.last_saved < 60:
            return
        self.db.execute('INSERT OR REPLACE INTO samples VALUES (?, ?, ?)', (now, float(watts), int(on)))
        self.db.commit()
        self.last_saved = now

    def history(self, period):
        windows = {'1h': (3600, 60), '24h': (86400, 300), '7d': (604800, 1800), '30d': (2592000, 7200)}
        duration, bucket = windows[period]
        since = int(time.time()) - duration
        rows = self.db.execute(
            'SELECT MAX(ts) AS ts, ROUND(AVG(watts), 2) AS watts FROM samples '
            'WHERE ts >= ? GROUP BY (ts / ?) ORDER BY ts', (since, bucket)
        ).fetchall()
        return {'range': period, 'bucket_seconds': bucket, 'points': [{'ts': ts, 'watts': watts} for ts, watts in rows]}

    def prune(self):
        self.db.execute('DELETE FROM samples WHERE ts < ?', (int(time.time()) - 90 * 86400,))
        self.db.commit()

    def close(self):
        self.db.close()


class Plug:
    def __init__(self, store):
        self.store = store
        self.device = None
        self.lock = asyncio.Lock()
        self.online = False
        self.is_on = None
        self.watts = None
        self.today_kwh = None
        self.month_kwh = None
        self.alias = 'Desk Power'
        self.updated_at = None
        self.error = 'Connecting to plug'

    def snapshot(self):
        return {
            'online': self.online,
            'on': self.is_on if self.online else None,
            'watts': self.watts if self.online else None,
            'today_kwh': self.today_kwh if self.online else None,
            'month_kwh': self.month_kwh if self.online else None,
            'alias': self.alias,
            'updated_at': self.updated_at,
            'error': self.error,
            'eur_per_kwh': EUR_PER_KWH,
            'off_max_watts': OFF_MAX_WATTS,
        }

    async def _connect(self):
        if self.device is None:
            self.device = await Discover.discover_single(
                TAPO_IP, username=TAPO_USERNAME, password=TAPO_PASSWORD
            )
            if self.device is None:
                raise ConnectionError('Tapo plug not found at configured IP')

    async def _update_locked(self):
        await self._connect()
        await self.device.update()
        energy = self.device.modules.get(Module.Energy)
        if energy is None:
            raise RuntimeError('Energy measurements not available for this device')
        self.is_on = bool(self.device.is_on)
        self.watts = energy.current_consumption
        self.today_kwh = energy.consumption_today
        self.month_kwh = energy.consumption_this_month
        self.alias = self.device.alias or 'Desk Power'
        self.updated_at = int(time.time())
        self.online = True
        self.error = None
        if self.watts is not None:
            self.store.save(self.watts, self.is_on)

    async def _reset_after_error(self, exception):
        LOG.warning('Tapo connection problem: %s', exception)
        self.online = False
        self.error = 'Plug unavailable. Check its Wi-Fi, IP and credentials'
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
            except Exception as ex:
                await self._reset_after_error(ex)

    async def set_power(self, turn_on):
        async with self.lock:
            try:
                # Always fetch a fresh reading before issuing a potentially destructive OFF
                await asyncio.wait_for(self._update_locked(), timeout=15)
            except Exception as ex:
                await self._reset_after_error(ex)
                raise web.HTTPServiceUnavailable(text='Plug is unreachable') from ex

            if not turn_on:
                if self.watts is None:
                    raise web.HTTPConflict(text='No live wattage measurement. Refusing OFF for safety')
                if self.watts > OFF_MAX_WATTS:
                    raise web.HTTPConflict(text=f'OFF blocked: desk is using {self.watts:.1f} W (limit {OFF_MAX_WATTS:g} W). Shut Zeus down first')

            try:
                await asyncio.wait_for(self.device.turn_on() if turn_on else self.device.turn_off(), timeout=12)
                await asyncio.wait_for(self._update_locked(), timeout=15)
            except Exception as ex:
                await self._reset_after_error(ex)
                raise web.HTTPServiceUnavailable(text='Switch request failed; check the Tapo app before retrying') from ex
            return self.snapshot()

    async def close(self):
        if self.device is not None:
            with suppress(Exception):
                await self.device.disconnect()


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
    response = web.json_response(request.app['plug'].snapshot())
    response.headers['Cache-Control'] = 'no-store'
    return response


async def get_history(request):
    period = request.query.get('range', '24h')
    if period not in ('1h', '24h', '7d', '30d'):
        raise web.HTTPBadRequest(text='Supported ranges: 1h, 24h, 7d, 30d')
    response = web.json_response(request.app['store'].history(period))
    response.headers['Cache-Control'] = 'no-store'
    return response


async def set_power(request):
    try:
        body = await request.json()
    except (ValueError, web.HTTPException):
        raise web.HTTPBadRequest(text='Expected JSON like {"on": true}')
    if not isinstance(body, dict) or type(body.get('on')) is not bool:
        raise web.HTTPBadRequest(text='Expected JSON like {"on": true}')
    return web.json_response(await request.app['plug'].set_power(body['on']))


async def index(request):
    return web.FileResponse(ROOT / 'static' / 'index.html', headers={'Cache-Control': 'no-cache'})


async def background_poll(app):
    plug = app['plug']
    app['poll_task'] = asyncio.create_task(poll_forever(plug, app['store']))


async def poll_forever(plug, store):
    last_prune = 0
    while True:
        await plug.refresh()
        if time.monotonic() - last_prune > 86400:
            store.prune()
            last_prune = time.monotonic()
        await asyncio.sleep(POLL_SECONDS)


async def shutdown(app):
    app['poll_task'].cancel()
    with suppress(asyncio.CancelledError):
        await app['poll_task']
    await app['plug'].close()
    app['store'].close()


def make_app():
    store = Store(DB_PATH)
    app = web.Application(middlewares=[security], client_max_size=4096)
    app['store'] = store
    app['plug'] = Plug(store)
    app.router.add_get('/', index)
    app.router.add_get('/api/state', get_state)
    app.router.add_get('/api/history', get_history)
    app.router.add_post('/api/power', set_power)
    app.router.add_static('/', ROOT / 'static', show_index=False)
    app.on_startup.append(background_poll)
    app.on_cleanup.append(shutdown)
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if not all((TAPO_IP, TAPO_USERNAME, TAPO_PASSWORD, DASH_USER, DASH_PASSWORD)):
        raise SystemExit('Configure TAPO_IP, TAPO_USERNAME, TAPO_PASSWORD, DASH_USER, DASH_PASSWORD in .env')
    LOG.info('Serving Desk Power at http://%s:%s', HTTP_HOST, HTTP_PORT)
    web.run_app(make_app(), host=HTTP_HOST, port=HTTP_PORT, access_log=None)
