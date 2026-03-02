import asyncio
import csv
import json
import logging
import time
import os
import subprocess
from datetime import datetime, timezone, timedelta
from tapo import ApiClient
from wakeonlan import send_magic_packet
from dotenv import load_dotenv

# ==========================================
# 📋 LOGGING SETUP
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler()]  # stderr — unbuffered, systemd-friendly
)
log = logging.getLogger('powersentry')

# Load variables from the .env file
load_dotenv()

# ==========================================
# ⚙️ CONFIGURATION (Loaded from .env)
# ==========================================
TAPO_IP = os.getenv("TAPO_IP")
TAPO_EMAIL = os.getenv("TAPO_EMAIL")
TAPO_PASS = os.getenv("TAPO_PASS")

PC_MAC = os.getenv("PC_MAC")
PC_IP = os.getenv("PC_IP")
PC_USER = os.getenv("PC_USER")
SHUTDOWN_CMD = os.getenv("SHUTDOWN_CMD")

# Virtual BMS Math (casting strings to integers/floats)
BATTERY_VOLTAGE = float(os.getenv("BATTERY_VOLTAGE"))
BATTERY_AH = float(os.getenv("BATTERY_AH"))
CHARGE_AMPS = float(os.getenv("CHARGE_AMPS"))
AVG_LOAD_WATTS = float(os.getenv("AVG_LOAD_WATTS"))
MAX_OUTAGE_MINUTES = int(os.getenv("MAX_OUTAGE_MINUTES"))

# Lead-Acid BMS Tuning (optional — sensible defaults if not in .env)
INVERTER_EFFICIENCY = float(os.getenv("INVERTER_EFFICIENCY", "0.85"))
SELF_DISCHARGE_PCT_PER_MONTH = float(os.getenv("SELF_DISCHARGE_PCT_MONTH", "3.0"))
USABLE_CAPACITY_FACTOR = float(os.getenv("USABLE_CAPACITY_FACTOR", "0.80"))
CHARGE_EFFICIENCY = float(os.getenv("CHARGE_EFFICIENCY", "0.80"))

# Capacity Aging — lead-acid permanently loses ~3-5% capacity per year
# Set BATTERY_INSTALL_DATE to enable (ISO format: 2024-01-15)
BATTERY_INSTALL_DATE = os.getenv("BATTERY_INSTALL_DATE", "")
CAPACITY_LOSS_PCT_PER_YEAR = float(os.getenv("CAPACITY_LOSS_PCT_PER_YEAR", "5.0"))

# Water Topup Reminder — flooded lead-acid needs periodic water refill
# Set to 0 to disable (sealed/AGM batteries don't need water)
WATER_TOPUP_INTERVAL_DAYS = int(os.getenv("WATER_TOPUP_INTERVAL_DAYS", "0"))

# Derived BMS Constants
TOTAL_CAPACITY_WH = BATTERY_VOLTAGE * BATTERY_AH
_BASE_USABLE_WH = TOTAL_CAPACITY_WH * USABLE_CAPACITY_FACTOR

def _compute_aging_factor():
    """
    Lead-acid capacity aging: permanent Ah loss over time.

    Unlike self-discharge (recoverable by recharging), aging is irreversible —
    the plates sulfate, active material sheds, and the battery physically holds
    less energy. Typical sealed lead-acid loses ~3-5%/year; flooded ~5-7%/year.
    Floor at 50% — below that the battery should be replaced.
    """
    if not BATTERY_INSTALL_DATE:
        return 1.0
    try:
        install = datetime.strptime(BATTERY_INSTALL_DATE, "%Y-%m-%d")
        age_years = (datetime.now() - install).days / 365.25
        factor = max(1.0 - (CAPACITY_LOSS_PCT_PER_YEAR / 100.0) * age_years, 0.50)
        return factor
    except ValueError:
        log.warning(f"Invalid BATTERY_INSTALL_DATE '{BATTERY_INSTALL_DATE}', ignoring aging")
        return 1.0

AGING_FACTOR = _compute_aging_factor()
USABLE_CAPACITY_WH = _BASE_USABLE_WH * AGING_FACTOR

# Battery-side drain is higher than AC load due to inverter conversion loss
DISCHARGE_WH_PER_MIN = (AVG_LOAD_WATTS / INVERTER_EFFICIENCY) / 60
CHARGE_WH_PER_MIN = (BATTERY_VOLTAGE * CHARGE_AMPS * CHARGE_EFFICIENCY) / 60

# Self-discharge: convert %/month → Wh/minute
# Sealed lead-acid ~3-5%/month, flooded ~10-15%/month
SELF_DISCHARGE_WH_PER_MIN = (SELF_DISCHARGE_PCT_PER_MONTH / 100.0) * TOTAL_CAPACITY_WH / (30 * 24 * 60)

STATE_FILE = "./state.json"
EVENT_LOG = "./events.csv"
TAPO_TIMEOUT = 15  # seconds — fail fast if plug unreachable

# IST timezone (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

# ==========================================
# 💾 STATE & EVENT MANAGEMENT
# ==========================================
def now_ist():
    """Current time in IST as a formatted string."""
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

def log_csv_event(event, duration_mins=None, battery_pct=None, details=""):
    """Append an event row to the CSV log."""
    file_exists = os.path.exists(EVENT_LOG)
    with open(EVENT_LOG, 'a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "event", "duration_mins", "battery_pct", "details"])
        writer.writerow([
            now_ist(),
            event,
            round(duration_mins, 1) if duration_mins is not None else "",
            round(battery_pct, 1) if battery_pct is not None else "",
            details
        ])

def load_state():
    default = {
        "pc_is_shut_down": False,       # True ONLY if PowerSentry successfully shut it down
        "shutdown_attempted": False,    # True once shutdown window reached (prevents retry spam)
        "current_wh": USABLE_CAPACITY_WH,  # Assume full battery on first run
        "power_cut_time": None,
        "last_tick_time": time.time(),   # Track elapsed time between ticks
        "last_water_topup": None,        # Epoch timestamp of last water topup (user-managed)
        "last_water_reminder": 0         # Epoch timestamp of last reminder log (anti-spam)
    }
    if not os.path.exists(STATE_FILE):
        return default
    with open(STATE_FILE, 'r') as f:
        state = json.load(f)
    # Backward compat: add any missing keys from default
    for key, val in default.items():
        if key not in state:
            state[key] = val
    return state

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

# ==========================================
# 🔋 VIRTUAL BMS — LEAD-ACID BATTERY MODEL
# ==========================================
def get_soc(state):
    """State of Charge as 0–100%."""
    return (state["current_wh"] / USABLE_CAPACITY_WH) * 100

def apply_charge(state, elapsed_mins):
    """
    Lead-acid CC/CV charge model with self-discharge.

    Real lead-acid chargers follow a 3-stage profile:
      Bulk   (SoC < 80%):  Full constant-current rate
      Absorb (80–95%):     Voltage held constant, current tapers linearly
      Float  (> 95%):      Trickle charge to offset self-discharge

    Self-discharge is always subtracted, even while charging.
    """
    soc = get_soc(state)

    if soc < 80:
        rate = CHARGE_WH_PER_MIN                                # Bulk — full CC rate
    elif soc < 95:
        taper = 1.0 - 0.75 * ((soc - 80) / 15)                 # Linear taper 100% → 25%
        rate = CHARGE_WH_PER_MIN * taper                        # Absorption
    else:
        rate = CHARGE_WH_PER_MIN * 0.10                         # Float/trickle

    charge_wh = rate * elapsed_mins
    self_discharge_wh = SELF_DISCHARGE_WH_PER_MIN * elapsed_mins
    state["current_wh"] = min(state["current_wh"] + charge_wh - self_discharge_wh, USABLE_CAPACITY_WH)
    state["current_wh"] = max(state["current_wh"], 0)

def apply_discharge(state, elapsed_mins):
    """
    Drain battery under inverter load + self-discharge.

    The inverter converts DC → AC at <100% efficiency, so the battery-side
    drain is higher than the AC load: drain = load_watts / inverter_efficiency.
    Self-discharge stacks on top.
    """
    load_wh = DISCHARGE_WH_PER_MIN * elapsed_mins
    self_discharge_wh = SELF_DISCHARGE_WH_PER_MIN * elapsed_mins
    state["current_wh"] = max(state["current_wh"] - load_wh - self_discharge_wh, 0)

def apply_self_discharge(state, elapsed_mins):
    """Apply self-discharge only (no load, no charge). Used for offline catchup."""
    loss_wh = SELF_DISCHARGE_WH_PER_MIN * elapsed_mins
    state["current_wh"] = max(state["current_wh"] - loss_wh, 0)

# ==========================================
# 🖥️ PC REACHABILITY & SHUTDOWN
# ==========================================
def is_pc_reachable():
    """Quick ping check — is the PC actually powered on and on the network?"""
    result = subprocess.run(
        ["ping", "-c", "1", "-W", "3", PC_IP],
        capture_output=True
    )
    return result.returncode == 0

def shutdown_pc():
    """
    Attempt to shut down the PC via SSH.

    Returns True only if the PC was reachable AND shutdown command succeeded.
    This matters because:
      - If PC is already off (user shut it down at night) → return False
        → pc_is_shut_down stays False → no WOL on power restore
      - If SSH fails (key issue, permission, etc.) → return False
        → shutdown_attempted prevents retry spam, but no false WOL
    """
    if not is_pc_reachable():
        log.info("PC unreachable (already off or not on network) — skipping shutdown")
        return False

    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
         f"{PC_USER}@{PC_IP}", SHUTDOWN_CMD],
        capture_output=True
    )
    if result.returncode == 0:
        log.info("SSH shutdown command succeeded")
        return True
    else:
        stderr = result.stderr.decode(errors='replace').strip()
        log.warning(f"SSH shutdown failed (exit={result.returncode}): {stderr[:200]}")
        return False

# ==========================================
# 💧 WATER TOPUP REMINDER
# ==========================================
def check_water_topup(state):
    """
    Log a reminder if water topup is overdue for flooded lead-acid batteries.

    Flooded lead-acid batteries lose water through electrolysis during charging
    and need periodic distilled water refill (every 2-3 months typically).
    Sealed/AGM/Gel batteries don't need this — set WATER_TOPUP_INTERVAL_DAYS=0.

    To record a topup, update state.json:
      "last_water_topup": <current epoch timestamp>
    Or delete the key to reset the timer.
    """
    if WATER_TOPUP_INTERVAL_DAYS <= 0:
        return

    if state["last_water_topup"] is None:
        # First run — start the timer from now
        state["last_water_topup"] = time.time()
        log.info(f"Water topup timer started — next reminder in {WATER_TOPUP_INTERVAL_DAYS} days")
        return

    days_since = (time.time() - state["last_water_topup"]) / 86400
    if days_since >= WATER_TOPUP_INTERVAL_DAYS:
        # Only log once per day to avoid spam
        last_reminder = state.get("last_water_reminder", 0)
        if (time.time() - last_reminder) >= 86400:
            overdue = days_since - WATER_TOPUP_INTERVAL_DAYS
            log.warning(f"WATER TOPUP DUE — {days_since:.0f} days since last topup ({overdue:.0f} days overdue)")
            log_csv_event("WATER_TOPUP_DUE", details=f"{days_since:.0f}d since topup, {overdue:.0f}d overdue")
            state["last_water_reminder"] = time.time()

# ==========================================
# 🧠 THE WATCHTOWER LOGIC LOOP
# ==========================================
async def main():
    log.info("Starting PowerSentry Watchtower...")
    log.info(f"Tapo plug IP: {TAPO_IP}")
    log.info(f"PC target: {PC_USER}@{PC_IP} (MAC: {PC_MAC})")
    log.info(f"Battery model: {TOTAL_CAPACITY_WH:.0f}Wh total, {USABLE_CAPACITY_WH:.0f}Wh usable")
    log.info(f"Inverter efficiency: {INVERTER_EFFICIENCY*100:.0f}%, Self-discharge: {SELF_DISCHARGE_PCT_PER_MONTH}%/month")
    if AGING_FACTOR < 1.0:
        log.info(f"Capacity aging: {AGING_FACTOR*100:.1f}% of original (install: {BATTERY_INSTALL_DATE}, {CAPACITY_LOSS_PCT_PER_YEAR}%/yr loss)")
    log.info(f"Max outage before shutdown: {MAX_OUTAGE_MINUTES} mins")
    if WATER_TOPUP_INTERVAL_DAYS > 0:
        log.info(f"Water topup reminder: every {WATER_TOPUP_INTERVAL_DAYS} days")

    state = load_state()

    # --- Offline self-discharge catchup ---
    # If the service was down for a while, apply self-discharge for the gap
    offline_gap_mins = (time.time() - state["last_tick_time"]) / 60
    if offline_gap_mins > 2:
        before_wh = state["current_wh"]
        apply_self_discharge(state, offline_gap_mins)
        log.info(f"Offline catchup: {offline_gap_mins:.1f}min gap, self-discharge {before_wh - state['current_wh']:.2f}Wh, battery now {get_soc(state):.1f}%")
        log_csv_event("OFFLINE_CATCHUP", duration_mins=offline_gap_mins, battery_pct=get_soc(state),
                      details=f"self-discharge for {offline_gap_mins:.0f}min offline")

    # --- Startup recovery ---
    # If PC was shut down BY US during a previous outage but power_cut_time is cleared
    # (meaning grid came back while service was down), we need to WOL now.
    # pc_is_shut_down is only True if we successfully SSH'd shutdown — never for
    # PCs the user turned off manually, so this won't wake a deliberately-off PC.
    if state["pc_is_shut_down"] and state["power_cut_time"] is None:
        log.info("STARTUP RECOVERY — PC was shut down by PowerSentry in previous outage, grid appears restored")
        send_magic_packet(PC_MAC)
        log.info(f"WOL PACKET SENT — waking {PC_MAC}")
        log_csv_event("STARTUP_WOL", battery_pct=get_soc(state), details="recovery after service restart")
        state["pc_is_shut_down"] = False
        state["shutdown_attempted"] = False
        save_state(state)

    log.info(f"State loaded: battery={state['current_wh']:.1f}Wh ({get_soc(state):.1f}%), pc_shut_down={state['pc_is_shut_down']}")

    client = ApiClient(TAPO_EMAIL, TAPO_PASS, timeout_s=TAPO_TIMEOUT)
    log.info("Tapo client created. Entering main loop.")

    while True:
        # --- Time-aware tick ---
        now = time.time()
        elapsed_mins = (now - state["last_tick_time"]) / 60
        elapsed_mins = max(elapsed_mins, 0.1)  # Safety: never zero/negative
        state["last_tick_time"] = now

        try:
            # Try to contact the Smart Plug (timeout handled by ApiClient)
            device = await client.p110(TAPO_IP)
            device_info = await device.get_device_info()
            uptime = device_info.on_time

            # Charge the Virtual Battery (CC/CV model with self-discharge)
            apply_charge(state, elapsed_mins)
            battery_pct = get_soc(state)

            log.info(f"Grid OK — battery={battery_pct:.1f}%, uptime={uptime}s")

            # POWER IS ON (Grid Restored / Charging)
            if state["power_cut_time"] is not None:
                cut_duration = (now - state["power_cut_time"]) / 60

                # Check for Wi-Fi Blip False Positive
                if uptime < 300:  # Plug rebooted recently, so it was a real power cut
                    log.info(f"POWER RESTORED — outage lasted {round(cut_duration)} mins")
                    log_csv_event("POWER_RESTORED", duration_mins=cut_duration, battery_pct=battery_pct)

                    # Only wake the PC if WE shut it down (not if user turned it off)
                    if state["pc_is_shut_down"]:
                        send_magic_packet(PC_MAC)
                        log.info(f"WOL PACKET SENT — waking {PC_MAC}")
                        log_csv_event("WOL_SENT", battery_pct=battery_pct, details=PC_MAC)
                        state["pc_is_shut_down"] = False
                else:
                    log.info("WIFI BLIP DETECTED — plug never lost power, ignoring")
                    log_csv_event("WIFI_BLIP", duration_mins=cut_duration, battery_pct=battery_pct)

                state["power_cut_time"] = None
                state["shutdown_attempted"] = False  # Reset for next outage

            # Check water topup reminder
            check_water_topup(state)

            save_state(state)

        except Exception as e:
            # Tapo library wraps all connection failures (timeout, unreachable, etc.)
            # as generic Exceptions with verbose Rust/reqwest internals — log cleanly
            log.warning(f"Plug unreachable — {type(e).__name__}")

            # Treat as power out — drain battery, trigger shutdown if needed
            if state["power_cut_time"] is None:
                state["power_cut_time"] = now
                log.info("POWER CUT DETECTED")
                battery_pct = get_soc(state)
                log_csv_event("POWER_CUT", battery_pct=battery_pct)

            outage_duration_mins = (now - state["power_cut_time"]) / 60

            # Drain the Virtual Battery (inverter-adjusted + self-discharge)
            apply_discharge(state, elapsed_mins)
            battery_pct = get_soc(state)

            log.info(f"Grid DOWN — battery={battery_pct:.1f}%, outage={outage_duration_mins:.1f}min")

            # THE FAILSAFE (Shutdown PC if limit reached)
            # shutdown_attempted prevents retrying every tick after the first attempt
            if outage_duration_mins >= MAX_OUTAGE_MINUTES and not state["shutdown_attempted"]:
                state["shutdown_attempted"] = True

                success = shutdown_pc()
                if success:
                    # PC was running and we shut it down — WOL on power restore
                    state["pc_is_shut_down"] = True
                    log.info("SHUTDOWN INITIATED — outage limit reached, saving 3090 Rig")
                    log_csv_event("SHUTDOWN", duration_mins=outage_duration_mins, battery_pct=battery_pct,
                                  details="outage limit reached")
                else:
                    # PC was already off (user shut down at night?) or SSH failed
                    # Do NOT set pc_is_shut_down — we must not WOL a deliberately-off PC
                    log_csv_event("SHUTDOWN_SKIPPED", duration_mins=outage_duration_mins, battery_pct=battery_pct,
                                  details="PC already off or unreachable")

            save_state(state)

        # Sleep for exactly 60 seconds
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
