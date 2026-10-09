"""API tests for two simulated P115s; no device or Wi-Fi required."""
import base64
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
os.environ.update({
    'TAPO_PLUGS': 'zeus@192.0.2.55,monitors@192.0.2.56',
    'TAPO_USERNAME': 'test@example.com', 'TAPO_PASSWORD': 'fake-password',
    'DASH_USER': 'test', 'DASH_PASSWORD': 'dash-secret',
    'OFF_MAX_WATTS': '30',
})


class FakeEnergy:
    def __init__(self, watts, today, month):
        self.current_consumption = watts
        self.consumption_today = today
        self.consumption_this_month = month


class Module:
    Energy = 'Energy'


class FakeP115:
    def __init__(self, name, watts, today, month):
        self.modules = {Module.Energy: FakeEnergy(watts, today, month)}
        self.is_on = True
        self.alias = name
        self.offline = False

    async def update(self):
        if self.offline:
            raise ConnectionError('Plug disappeared')

    async def turn_on(self):
        self.is_on = True

    async def turn_off(self):
        self.is_on = False

    async def disconnect(self):
        pass


devices = {
    '192.0.2.55': FakeP115('Zeus PC', 92.4, 1.25, 31.1),
    '192.0.2.56': FakeP115('Monitors', 28.2, 0.5, 9.2),
}


class Discover:
    @staticmethod
    async def discover_single(ip, username, password):
        assert username == os.environ['TAPO_USERNAME']
        return devices[ip]


sys.modules['kasa'] = types.SimpleNamespace(Discover=Discover, Module=Module)
spec = importlib.util.spec_from_file_location('desk_power_server', ROOT / 'server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(self.tmp.name) / 'history.sqlite'
        for ip, watts, today, month in [('192.0.2.55', 92.4, 1.25, 31.1), ('192.0.2.56', 28.2, 0.5, 9.2)]:
            p = devices[ip]
            p.is_on = True
            p.offline = False
            p.modules[Module.Energy] = FakeEnergy(watts, today, month)
        self.srv = TestServer(server.make_app())
        self.cli = TestClient(self.srv)
        await self.cli.start_server()
        for p in self.srv.app['plugs'].values():
            await p.refresh()
        auth = base64.b64encode(b'test:dash-secret').decode()
        self.headers = {'Authorization': f'Basic {auth}'}

    async def asyncTearDown(self):
        await self.cli.close()
        self.tmp.cleanup()

    async def test_auth_required(self):
        resp = await self.cli.get('/api/state')
        self.assertEqual(resp.status, 401)

    async def test_combined_readings(self):
        resp = await self.cli.get('/api/state', headers=self.headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data['online'])
        self.assertEqual(data['plug_count'], 2)
        self.assertEqual(data['online_count'], 2)
        self.assertAlmostEqual(data['watts'], 120.6)
        self.assertEqual(data['today_kwh'], 1.75)
        self.assertEqual(data['month_kwh'], 40.3)
        self.assertEqual([p['id'] for p in data['plugs']], ['zeus', 'monitors'])

    async def test_two_independent_samples(self):
        rows = self.srv.app['store'].db.execute('SELECT plug_id, watts FROM plug_samples ORDER BY plug_id').fetchall()
        self.assertEqual(rows, [('monitors', 28.2), ('zeus', 92.4)])

    async def test_off_blocked_for_zeus_above_threshold(self):
        resp = await self.cli.post('/api/plugs/zeus/power', headers=self.headers, json={'on': False})
        self.assertEqual(resp.status, 409)
        self.assertTrue(devices['192.0.2.55'].is_on)

    async def test_other_outlet_switches_independently(self):
        resp = await self.cli.post('/api/plugs/monitors/power', headers=self.headers, json={'on': False})
        self.assertEqual(resp.status, 200)
        self.assertFalse(devices['192.0.2.56'].is_on)
        self.assertTrue(devices['192.0.2.55'].is_on)
        resp = await self.cli.post('/api/plugs/monitors/power', headers=self.headers, json={'on': True})
        self.assertEqual(resp.status, 200)
        self.assertTrue(devices['192.0.2.56'].is_on)

    async def test_partial_totals_are_not_misleading(self):
        devices['192.0.2.56'].offline = True
        await self.srv.app['plugs']['monitors'].refresh()
        resp = await self.cli.get('/api/state', headers=self.headers)
        data = await resp.json()
        self.assertFalse(data['online'])
        self.assertEqual(data['online_count'], 1)
        self.assertIsNone(data['watts'])
        self.assertIsNone(data['today_kwh'])
        self.assertTrue(data['plugs'][0]['online'])
        self.assertFalse(data['plugs'][1]['online'])

    async def test_graph_history_combined_and_individual(self):
        for slug, expected in [('total', 120.6), ('zeus', 92.4), ('monitors', 28.2)]:
            resp = await self.cli.get('/api/history?range=24h&plug=' + slug, headers=self.headers)
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data['plug'], slug)
            self.assertGreaterEqual(len(data['points']), 1)
            self.assertAlmostEqual(data['points'][-1]['watts'], expected)

    async def test_invalid_plug_and_foreign_origin(self):
        resp = await self.cli.post('/api/plugs/notfound/power', headers=self.headers, json={'on': True})
        self.assertEqual(resp.status, 404)
        resp = await self.cli.post('/api/plugs/monitors/power', headers={
            **self.headers, 'Origin': 'https://evil.example'}, json={'on': False})
        self.assertEqual(resp.status, 403)
        self.assertTrue(devices['192.0.2.56'].is_on)

    async def test_frontend_and_invalid_history_selector(self):
        resp = await self.cli.get('/', headers=self.headers)
        self.assertEqual(resp.status, 200)
        self.assertIn('TOTAL CURRENT DRAW', await resp.text())
        resp = await self.cli.get('/api/history?plug=invalid', headers=self.headers)
        self.assertEqual(resp.status, 400)


if __name__ == '__main__':
    unittest.main()
