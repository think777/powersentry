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

# Derived BMS Constants
TOTAL_CAPACITY_WH = BATTERY_VOLTAGE * BATTERY_AH
USABLE_CAPACITY_WH = TOTAL_CAPACITY_WH * 0.80  # Keep 20% safe buffer
DISCHARGE_WH_PER_MIN = AVG_LOAD_WATTS / 60
CHARGE_WH_PER_MIN = (BATTERY_VOLTAGE * CHARGE_AMPS * 0.80) / 60 # 80% charging efficiency

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
    if not os.path.exists(STATE_FILE):
        return {
            "pc_is_shut_down": False,
            "current_wh": USABLE_CAPACITY_WH, # Assume full battery on first run
            "power_cut_time": None
        }
    with open(STATE_FILE, 'r') as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

# ==========================================
# 🧠 THE WATCHTOWER LOGIC LOOP
# ==========================================
async def main():
    log.info("Starting PowerSentry Watchtower...")
    log.info(f"Tapo plug IP: {TAPO_IP}")
    log.info(f"PC target: {PC_USER}@{PC_IP} (MAC: {PC_MAC})")
    log.info(f"Battery model: {TOTAL_CAPACITY_WH:.0f}Wh total, {USABLE_CAPACITY_WH:.0f}Wh usable")
    log.info(f"Max outage before shutdown: {MAX_OUTAGE_MINUTES} mins")

    state = load_state()
    log.info(f"State loaded: battery={state['current_wh']:.1f}Wh, pc_shut_down={state['pc_is_shut_down']}")

    client = ApiClient(TAPO_EMAIL, TAPO_PASS, timeout_s=TAPO_TIMEOUT)
    log.info("Tapo client created. Entering main loop.")

    while True:
        try:
            # Try to contact the Smart Plug (timeout handled by ApiClient)
            device = await client.p110(TAPO_IP)
            device_info = await device.get_device_info()
            uptime = device_info.on_time

            # Charge the Virtual Battery
            state["current_wh"] = min(state["current_wh"] + CHARGE_WH_PER_MIN, USABLE_CAPACITY_WH)
            battery_pct = (state["current_wh"] / USABLE_CAPACITY_WH) * 100

            log.info(f"Grid OK — battery={battery_pct:.1f}%, uptime={uptime}s")

            # POWER IS ON (Grid Restored / Charging)
            if state["power_cut_time"] is not None:
                cut_duration = int(time.time() - state["power_cut_time"]) / 60

                # Check for Wi-Fi Blip False Positive
                if uptime < 300: # Plug rebooted recently, so it was a real power cut
                    log.info(f"POWER RESTORED — outage lasted {round(cut_duration)} mins")
                    log_csv_event("POWER_RESTORED", duration_mins=cut_duration, battery_pct=battery_pct)

                    # Do we need to wake the PC?
                    if state["pc_is_shut_down"]:
                        send_magic_packet(PC_MAC)
                        log.info(f"WOL PACKET SENT — waking {PC_MAC}")
                        log_csv_event("WOL_SENT", battery_pct=battery_pct, details=PC_MAC)
                        state["pc_is_shut_down"] = False
                else:
                    log.info("WIFI BLIP DETECTED — plug never lost power, ignoring")
                    log_csv_event("WIFI_BLIP", duration_mins=cut_duration, battery_pct=battery_pct)

                state["power_cut_time"] = None

            save_state(state)

        except Exception as e:
            # Tapo library wraps all connection failures (timeout, unreachable, etc.)
            # as generic Exceptions with verbose Rust/reqwest internals — log cleanly
            log.warning(f"Plug unreachable — {type(e).__name__}")

            # Treat as power out — drain battery, trigger shutdown if needed
            if state["power_cut_time"] is None:
                state["power_cut_time"] = time.time()
                log.info("POWER CUT DETECTED")
                battery_pct = (state["current_wh"] / USABLE_CAPACITY_WH) * 100
                log_csv_event("POWER_CUT", battery_pct=battery_pct)

            outage_duration_mins = (time.time() - state["power_cut_time"]) / 60

            # Drain the Virtual Battery
            state["current_wh"] = max(state["current_wh"] - DISCHARGE_WH_PER_MIN, 0)
            battery_pct = (state["current_wh"] / USABLE_CAPACITY_WH) * 100

            log.info(f"Grid DOWN — battery={battery_pct:.1f}%, outage={outage_duration_mins:.1f}min")

            # THE FAILSAFE (Shutdown PC if limit reached)
            if outage_duration_mins >= MAX_OUTAGE_MINUTES and not state["pc_is_shut_down"]:
                log.info("SHUTDOWN INITIATED — outage limit reached, saving 3090 Rig")
                log_csv_event("SHUTDOWN", duration_mins=outage_duration_mins, battery_pct=battery_pct, details="outage limit reached")
                subprocess.run(["ssh", f"{PC_USER}@{PC_IP}", SHUTDOWN_CMD], capture_output=True)
                state["pc_is_shut_down"] = True

            save_state(state)

        # Sleep for exactly 60 seconds
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
