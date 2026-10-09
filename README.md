# Desk Power — Tiny Tapo P115 dashboard for Hermes

A small Python (aiohttp + python-kasa) service, a SQLite file, and plain HTML/CSS/JS. No Docker, Home Assistant, build tools, Node.js, or cloud dashboard. The plug is queried **locally** over Wi-Fi using `python-kasa`; TP-Link/Tapo credentials are needed for its local authentication but are not sent to the frontend.

## Features

- Live W, today/month kWh directly from the P115
- Estimated monthly electricity cost, configurable EUR/kWh
- On/off control with confirmation and a server-side wattage-based OFF safety block
- 1 hour, 24 hour, 7 day, 30 day wattage graphs saved in SQLite (1 sample/min, 90-day retention)
- Mobile-responsive, installable PWA *when served over HTTPS*
- HTTP Basic authentication, auto-reconnect, systemd auto-start

## Deploy on Hermes

1. Pair your Tapo P115 in the Tapo app first and verify that wattage shows there
2. Find the plug's IP in Fritz!Box → Home Network → Network, and reserve that IP so it doesn't change. The `TAPO_IP=10.10.0.50` in the example is **just a placeholder**
3. Copy/unzip this folder onto Hermes, e.g. `~/desk-power`
4. Install Python venv support: `sudo apt update && sudo apt install -y python3-venv`
5. From inside the project folder:

   ```bash
   cp .env.example .env
   chmod 600 .env
   nano .env
   # Set TAPO_IP, TAPO_USERNAME, TAPO_PASSWORD, DASH_PASSWORD and EUR_PER_KWH
   ./install.sh
   ```

6. Open **http://hermes.karmtin:8766** (or `http://10.10.0.10:8766`) on your LAN and enter your configured `DASH_USER` / `DASH_PASSWORD` in the browser password prompt
7. Check `sudo journalctl -u desk-power -f` for any error

When modifying `.env`: `sudo systemctl restart desk-power`

### Why the username and password?

Use the same TP-Link/Tapo account credentials you used in the Tapo app. These stay on Hermes in `.env` and are used for local device authentication. `DASH_PASSWORD` is a **separate dashboard password**. Create a unique, long password for it

### Local network & PWA caveat

`http://hermes.karmtin:8766` works as a normal web dashboard but **most browsers do not enable service workers/PWA offline installation on plain LAN HTTP**. Use an HTTPS reverse proxy if you want full installable PWA behavior on iPhone. If using Caddy, point it at `127.0.0.1:8766` and **keep access private** (e.g. Meshnet or trusted VPN). Do not publicly expose socket control. If running behind a reverse proxy with a different hostname, the proxy should preserve the original Host header

For better protection, set `HTTP_HOST=127.0.0.1` in `.env` once Caddy is configured, and only access the service via the proxy

**Important:** HTTP Basic credentials can be read by observers on unencrypted LAN traffic. Prefer HTTPS when using it on devices/networks you do not fully trust

### Protect Zeus and Hermes

Turning the P115 OFF removes mains power from **everything connected to it**. Shut down Zeus normally first; the dashboard rejects OFF when the latest wattage is over `OFF_MAX_WATTS` (default 30 W). The threshold is a precaution, not a guarantee that the PC is shut down. Never plug Hermes or critical networking gear into the controlled outlet. The Tapo app remains your fallback when Hermes is offline

### Network troubleshooting

Try discovery from Hermes (inside virtual environment) with the IP and same Tapo account credentials:

```bash
cd ~/desk-power
source .venv/bin/activate
kasa --username 'your-tapo-account@example.com' --host 10.10.0.50 state
```

Some firmware versions may need **Third-Party Compatibility** enabled in the Tapo app. Only enable this if you encounter an authentication problem, and check the security warning shown in the app. Update `python-kasa` if needed, since behavior can vary by firmware. Ensure plug and Hermes can reach each other directly over the LAN (no guest Wi-Fi/client isolation)

### Services

```bash
sudo systemctl status desk-power --no-pager
sudo systemctl restart desk-power
sudo journalctl -u desk-power -f
```

### API

- `GET /api/state`: `online`, `on`, `watts`, `today_kwh`, `month_kwh`, `eur_per_kwh`
- `GET /api/history?range=24h`: `points` with timestamp and wattage
- `POST /api/power` with JSON `{"on":true}` or `{"on":false}`

All endpoints require HTTP Basic authentication. The dashboard works without any API tokens embedded in client-side code

## Limitations

- Energy stats only if your P115 firmware exposes them correctly to `python-kasa`
- A full PWA needs a trusted HTTPS origin
- No historical data exists until Hermes starts logging
- This is not a guaranteed power-loss-free smart shutdown system. Do not use the socket to hard-power-off a running computer
