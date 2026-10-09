"""Tests for the API with a simulated P115; no actual device required."""
import asyncio
import base64
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
os.environ.update({
    'TAPO_IP': '192.0.2.55', 'TAPO_USERNAME': 'test@example.com',
    'TAPO_PASSWORD': 'fake-password', 'DASH_USER': 'test',
    'DASH_PASSWORD': 'dash-secret', 'OFF_MAX_WATTS': '30',
})

class Energy:
    current_consumption = 92.4
    consumption_today = 1.25
    consumption_this_month = 31.1

class Module:
    Energy = 'Energy'

class FakeP115:
    def __init__(self):
        self.modules = {Module.Energy: Energy()}
        self.is_on = True
        self.alias = 'Desk test plug'
    async def update(self): pass
    async def turn_on(self): self.is_on = True
    async def turn_off(self): self.is_on = False
    async def disconnect(self): pass

plug = FakeP115()
class Discover:
    @staticmethod
    async def discover_single(ip, username, password):
        assert ip == os.environ['TAPO_IP']
        return plug

sys.modules['kasa'] = types.SimpleNamespace(Discover=Discover, Module=Module)
spec = importlib.util.spec_from_file_location('desk_power_server', ROOT / 'server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(self.tmp.name) / 'history.sqlite'
        self.srv = TestServer(server.make_app())
        self.cli = TestClient(self.srv)
        await self.cli.start_server()
        await self.srv.app['plug'].refresh()
        credentials = base64.b64encode(b'test:dash-secret').decode()
        self.headers = {'Authorization': f'Basic {credentials}'}

    async def asyncTearDown(self):
        await self.cli.close()
        self.tmp.cleanup()
        plug.is_on = True
        plug.modules[Module.Energy].current_consumption = 92.4

    async def test_auth_required(self):
        resp = await self.cli.get('/api/state')
        self.assertEqual(resp.status, 401)

    async def test_energy_readings(self):
        resp = await self.cli.get('/api/state', headers=self.headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data['online'])
        self.assertEqual(data['watts'], 92.4)
        self.assertEqual(data['today_kwh'], 1.25)
        self.assertTrue(data['on'])

    async def test_off_is_blocked_above_threshold(self):
        resp = await self.cli.post('/api/power', headers={**self.headers,'Content-Type':'application/json'},json={'on':False})
        self.assertEqual(resp.status,409)
        self.assertTrue(plug.is_on)

    async def test_safe_switch(self):
        plug.modules[Module.Energy].current_consumption = 4.5
        resp = await self.cli.post('/api/power', headers={**self.headers,'Content-Type':'application/json'},json={'on':False})
        self.assertEqual(resp.status,200)
        self.assertFalse(plug.is_on)
        resp = await self.cli.post('/api/power', headers={**self.headers,'Content-Type':'application/json'},json={'on':True})
        self.assertEqual(resp.status,200)
        self.assertTrue(plug.is_on)

    async def test_foreign_origin_forbidden(self):
        resp = await self.cli.post('/api/power', headers={**self.headers,'Origin':'https://evil.example','Content-Type':'application/json'},json={'on':False})
        self.assertEqual(resp.status,403)
        self.assertTrue(plug.is_on)

    async def test_frontend_and_history(self):
        resp = await self.cli.get('/',headers=self.headers)
        self.assertEqual(resp.status,200)
        self.assertIn('CURRENT DRAW',await resp.text())
        resp = await self.cli.get('/api/history?range=24h',headers=self.headers)
        self.assertEqual(resp.status,200)
        self.assertEqual((await resp.json())['range'],'24h')

if __name__ == '__main__':
    unittest.main()
