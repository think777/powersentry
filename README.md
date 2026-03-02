# PowerSentry

Virtual BMS watchdog for inverter + lead-acid battery setups. Monitors grid power via a Tapo smart plug, tracks battery state-of-charge with a lead-acid charge/discharge model, and safely shuts down your PC before the battery dies — then wakes it back up when power returns.

## How It Works

```
Grid Power → Tapo P110 Smart Plug → Inverter → Lead-Acid Battery Bank → PC (3090 Rig)
                    ↑
              PowerSentry polls
              every 60 seconds
```

1. **Grid OK** — Plug reachable → charge the virtual battery (CC/CV model)
2. **Grid DOWN** — Plug unreachable → drain the virtual battery (inverter-adjusted)
3. **Outage exceeds limit** — SSH shutdown to the PC (only if PC is actually running)
4. **Power restored** — Wake-on-LAN magic packet to bring the PC back (only if we shut it down)

### Smart PC Lifecycle

PowerSentry distinguishes between "we shut the PC down" and "the user turned it off":

| Scenario | What Happens | WOL on Restore? |
|----------|-------------|-----------------|
| Outage → PC running → PowerSentry shuts it down | SSH shutdown succeeds, `pc_is_shut_down=true` | **Yes** |
| Outage → PC already off (user turned it off at night) | Ping fails, shutdown skipped | **No** |
| Outage → SSH fails (key issue, network) | Logged as `SHUTDOWN_SKIPPED` | **No** |
| Service restarts after outage → PC was shut down by us | Startup recovery sends WOL | **Yes** |
| Service restarts → PC off by user choice | `pc_is_shut_down=false`, no action | **No** |

The key invariant: `pc_is_shut_down` is only set to `true` when PowerSentry successfully SSH'd into a running PC and ran the shutdown command. This prevents waking a PC the user deliberately turned off.

### Virtual BMS Model

The battery simulation models real lead-acid behavior:

| Phase | SoC Range | Behavior |
|-------|-----------|----------|
| **Bulk** (CC) | 0–80% | Full constant-current charge rate |
| **Absorption** (CV) | 80–95% | Current tapers linearly (100% → 25%) |
| **Float** | 95–100% | Trickle charge (~10% of max rate) |
| **Self-discharge** | Always | ~3–5%/month for sealed, 10–15% for flooded |

**Discharge** accounts for inverter DC→AC conversion loss (default 85% efficiency), so a 500W AC load actually draws ~588W from the battery.

**Capacity aging** — lead-acid batteries permanently lose capacity over time as plates sulfate and active material sheds. Set `BATTERY_INSTALL_DATE` to enable. The model reduces usable Wh based on battery age (default 5%/year loss, floored at 50%). This is different from self-discharge: self-discharge is recoverable by recharging, aging is permanent.

**Time-aware ticks** — each loop measures actual elapsed time instead of assuming exactly 60s, so the model stays accurate even if the service is delayed or restarted.

**Offline catchup** — on startup, self-discharge is applied for any gap since the last tick, keeping SoC honest after reboots.

**Startup recovery** — if the PC was shut down *by PowerSentry* during a previous outage and the service restarts with grid power available, WOL is sent immediately without waiting for a power-cut/restore cycle.

### Water Topup Reminder

Flooded lead-acid batteries lose water through electrolysis during charging and need periodic distilled water refills (every 2–3 months typically). Set `WATER_TOPUP_INTERVAL_DAYS` to enable daily log warnings when a topup is overdue.

Sealed/AGM/Gel batteries don't need water — leave this at `0` (default).

To record a topup, set `last_water_topup` in `state.json` to the current epoch timestamp:

```bash
python3 -c "import time; print(int(time.time()))"
# Then edit state.json: "last_water_topup": <that number>
```

### Wi-Fi Blip Detection

Not every plug-unreachable event is a power cut. If the plug's uptime is >5 minutes when it becomes reachable again, the outage was just a Wi-Fi/network glitch — the plug never lost power. These are logged as `WIFI_BLIP` and don't trigger WOL.

## Setup

### Prerequisites

- Python 3.10+
- A [Tapo P110](https://www.tapo.com/product/smart-plug/tapo-p110/) smart plug on the same network
- SSH key-based auth to your target PC (no password prompts)
- Wake-on-LAN enabled in BIOS on the target PC

### Install

```bash
git clone https://github.com/think777/powersentry.git
cd powersentry
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env with your values
```

**Required variables:**

| Variable | Description | Example |
|----------|-------------|---------|
| `TAPO_IP` | Smart plug IP address | `192.168.1.100` |
| `TAPO_EMAIL` | Tapo account email | `you@email.com` |
| `TAPO_PASS` | Tapo account password | `yourpassword` |
| `PC_MAC` | Target PC MAC address | `AA:BB:CC:DD:EE:FF` |
| `PC_IP` | Target PC IP address | `192.168.1.50` |
| `PC_USER` | SSH username on target PC | `user` |
| `SHUTDOWN_CMD` | Shutdown command to run via SSH | `sudo shutdown -h now` |
| `BATTERY_VOLTAGE` | Nominal battery bank voltage | `24` |
| `BATTERY_AH` | Total Ah capacity | `320` |
| `CHARGE_AMPS` | Inverter/charger charge current | `15` |
| `AVG_LOAD_WATTS` | Average AC load in watts | `500` |
| `MAX_OUTAGE_MINUTES` | Minutes before PC shutdown | `240` (4 hours) |

**Optional lead-acid tuning** (defaults are sane for sealed batteries):

| Variable | Default | Description |
|----------|---------|-------------|
| `INVERTER_EFFICIENCY` | `0.85` | DC→AC efficiency (0.80–0.90) |
| `SELF_DISCHARGE_PCT_MONTH` | `3.0` | Self-discharge %/month (sealed=3–5, flooded=10–15) |
| `USABLE_CAPACITY_FACTOR` | `0.80` | Depth-of-discharge limit (keep 20% reserve) |
| `CHARGE_EFFICIENCY` | `0.80` | Coulombic charging efficiency |
| `BATTERY_INSTALL_DATE` | *(disabled)* | ISO date of battery install, e.g. `2024-01-15` |
| `CAPACITY_LOSS_PCT_PER_YEAR` | `5.0` | Permanent capacity loss %/year (sealed=3–5, flooded=5–7) |
| `WATER_TOPUP_INTERVAL_DAYS` | `0` | Reminder interval in days (0=disabled, 90=quarterly) |

### Run Manually

```bash
source .venv/bin/activate
python powersentry.py
```

### Deploy as systemd Service

```bash
# Copy the service file
sudo cp powersentry.service /etc/systemd/system/

# Reload systemd, enable, and start
sudo systemctl daemon-reload
sudo systemctl enable powersentry
sudo systemctl start powersentry

# Check status and logs
sudo systemctl status powersentry
journalctl -u powersentry -f
```

The service will:
- Start automatically on boot (after network is up)
- Restart on failure (30s delay)
- Log to journalctl (no file-based logging needed)
- Run with hardened permissions (read-only filesystem access except the project directory)

## State & Logs

**`state.json`** — persisted every tick, contains:
- `current_wh` — current battery energy level
- `pc_is_shut_down` — whether PowerSentry shut down the PC (never true for user-initiated shutdowns)
- `shutdown_attempted` — whether shutdown was attempted this outage (prevents retry spam)
- `power_cut_time` — timestamp of current outage (null if grid OK)
- `last_tick_time` — last loop timestamp (for time-aware ticks and offline catchup)
- `last_water_topup` — epoch timestamp of last water topup (user-managed)

**`events.csv`** — append-only event log:
- `POWER_CUT` — grid went down
- `POWER_RESTORED` — grid came back (with outage duration)
- `WIFI_BLIP` — network glitch, not a real outage
- `SHUTDOWN` — PC shutdown triggered (PC was running, SSH succeeded)
- `SHUTDOWN_SKIPPED` — shutdown window reached but PC was already off or unreachable
- `WOL_SENT` — Wake-on-LAN packet sent
- `STARTUP_WOL` — WOL sent on service startup (recovery)
- `OFFLINE_CATCHUP` — self-discharge applied for offline gap
- `WATER_TOPUP_DUE` — water topup reminder (daily until topup recorded)

## Battery Math Example

With the default config (24V × 320Ah bank):

```
Total capacity:     7,680 Wh
Usable (80% DoD):   6,144 Wh
AC load:              500 W
Battery-side drain:   588 W  (500W ÷ 0.85 inverter efficiency)
Runtime:           ~10.4 hrs  (6,144 ÷ 588)
Self-discharge:     ~7.7 Wh/day  (negligible vs load, matters when idle)
```

With `MAX_OUTAGE_MINUTES=240` (4 hours), the PC shuts down well before the battery is depleted, leaving ~58% reserve for the inverter, lights, fans, etc.

**After 2 years** (with `BATTERY_INSTALL_DATE` set and 5%/yr aging):

```
Aging factor:       90%
Usable capacity:    5,530 Wh  (was 6,144 Wh when new)
Runtime:           ~9.4 hrs   (down from 10.4 hrs)
```

## License

GPL-3.0 — see [LICENSE](LICENSE) for details.
