# Desk Power v1.1 — Tiny multi-Tapo P115 dashboard for Hermes

One small Python service, plain HTML/CSS/JS frontend, and an SQLite database. No Home Assistant, Docker, Node.js, or frontend build process. Supports **two or more Tapo P115 plugs**, polled concurrently over your local Wi-Fi

## Features

- Combined **live W**, daily/monthly **kWh**, estimated monthly **€** when *all* configured plugs are online
- A card per plug with its own wattage, usage, health, and independent ON/OFF button
- 1h, 24h, 7d, 30d graph: choose **Combined** or an individual plug
- Store a separate sample for each plug around once/minute in SQLite; retain 90 days
- If one plug is disconnected, combined totals/history do **not** pretend partial usage is the complete total
- Before switching any outlet OFF, query its current load and refuse OFF above `OFF_MAX_WATTS` (default 30 W)
- Auto-reconnect, Basic authentication, systemd auto-start, a responsive PWA shell on trusted HTTPS

## Hermes install

1. Pair **both P115s** in the Tapo app under the **same TP-Link account**
2. Look up their LAN IP addresses in Fritz!Box and reserve them in DHCP; each has a different IP
3. Copy this directory to `/srv/desk-power` or `~/desk-power`
4. Run inside the directory:

```bash
sudo apt update && sudo apt install -y python3-venv
cp .env.example .env
chmod 600 .env
nano .env
./install.sh
```

Example `.env`:

```env
TAPO_PLUGS=zeus@10.10.0.50,monitors@10.10.0.51
TAPO_USERNAME=your-tapo-account@example.com
TAPO_PASSWORD=replace-me
DASH_USER=admin
DASH_PASSWORD=replace-with-a-long-unique-password
HTTP_HOST=0.0.0.0
HTTP_PORT=8766
POLL_SECONDS=10
OFF_MAX_WATTS=30
EUR_PER_KWH=0.30
```

**Those IP addresses are placeholders**. Replace with the actual IPs of your plugs. Stable IDs (`zeus`, `monitors`) identify each plug's history even if an IP changes. The actual display names come from the Tapo app

The plug credentials remain on Hermes in `.env` and are never supplied to JavaScript. `DASH_PASSWORD` should be different from your Tapo password

Open **http://hermes.karmtin:8766** on the LAN, or Hermes' actual IP if `.karmtin` DNS doesn't resolve

```bash
sudo systemctl status desk-power --no-pager
sudo journalctl -u desk-power -f
# After changing .env:
sudo systemctl restart desk-power
```

## Upgrading from v1

Back up `.env` and `data/history.sqlite`, replace the app files, and update your `.env` to use `TAPO_PLUGS=...` for both devices (keep `TAPO_USERNAME`, `TAPO_PASSWORD`, and dashboard settings)

The old `samples` table in the SQLite database is **preserved** but not counted by v1.1 because the samples have no plug ID. New per-plug graphs start with the first v1.1 measurements. No previous database contents are deleted

If you already installed v1, the existing `desk-power` systemd service can run the updated Python service. Reinstall only if the service path changes

## API

All endpoints require HTTP Basic authentication

- `GET /api/state` — combined totals, `online_count`, `plug_count`, and `plugs[]` with individual readings
- `GET /api/history?range=24h&plug=total` — combined points for buckets containing samples from all configured plugs
- `GET /api/history?range=24h&plug=zeus` — points for one plug
- `POST /api/plugs/zeus/power` with JSON `{"on": true}` or `{"on": false}` — individual plug control

Example test after installation:

```bash
curl -u 'admin:your-dashboard-password' http://127.0.0.1:8766/api/state
```

## Safety and security

**Never cut mains power to Zeus while it's running**, even if its momentary power draw falls below the cutoff. The 30 W limit is an additional safeguard, **not** a guaranteed shutdown detector. The dashboard has no "all off" button on purpose. **Never** connect Hermes, your router, or critical network equipment to either controlled outlet

Keep the dashboard on trusted LAN / Meshnet. Basic authentication over ordinary HTTP is unencrypted; use **trusted HTTPS** (e.g. private Caddy reverse proxy) before accessing it from untrusted networks. PWA install/service workers typically require HTTPS on phones. Do not open a public port to this service

Tapo firmware behavior varies. If discovery/authentication fails, check IPs, Wi-Fi client isolation, and the Tapo app's Third-Party Compatibility option (it has security implications), or update `python-kasa`. The plugs and Hermes must be reachable over LAN

## Test without physical plugs

```bash
python -m unittest discover -s tests -v
```

The test suite uses two simulated independent P115 devices. To validate the real setup, inspect the live readings and switching in the Tapo app and compare them to the dashboard
