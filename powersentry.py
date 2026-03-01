import asyncio
import json
import logging
import sqlite3
import time
import os
import subprocess
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

# File Paths
DB_PATH = "./watchtower.db"
STATE_FILE = "./state.json"

# Derived BMS Constants
TOTAL_CAPACITY_WH = BATTERY_VOLTAGE * BATTERY_AH
USABLE_CAPACITY_WH = TOTAL_CAPACITY_WH * 0.80  # Keep 20% safe buffer
DISCHARGE_WH_PER_MIN = AVG_LOAD_WATTS / 60
CHARGE_WH_PER_MIN = (BATTERY_VOLTAGE * CHARGE_AMPS * 0.80) / 60 # 80% charging efficiency

# ==========================================
# 🗄️ DATABASE & STATE MANAGEMENT
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS grid_telemetry
                 (timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, voltage REAL, is_power_on BOOLEAN, battery_percent REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS watchtower_events
                 (timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, event_type TEXT, details TEXT)''')
    conn.commit()
    conn.close()

def log_telemetry(voltage, is_power_on, battery_percent):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO grid_telemetry (voltage, is_power_on, battery_percent) VALUES (?, ?, ?)", 
              (voltage, is_power_on, round(battery_percent, 2)))
    conn.commit()
    conn.close()

def log_event(event_type, details=""):
    log.info(f"{event_type} - {details}")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO watchtower_events (event_type, details) VALUES (?, ?)", (event_type, details))
    conn.commit()
    conn.close()

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
TAPO_TIMEOUT = 15  # seconds — fail fast if plug unreachable

async def main():
    log.info("Starting PowerSentry Watchtower...")
    log.info(f"Tapo plug IP: {TAPO_IP}")
    log.info(f"PC target: {PC_USER}@{PC_IP} (MAC: {PC_MAC})")
    log.info(f"Battery model: {TOTAL_CAPACITY_WH:.0f}Wh total, {USABLE_CAPACITY_WH:.0f}Wh usable")
    log.info(f"Max outage before shutdown: {MAX_OUTAGE_MINUTES} mins")

    log.info("Initializing database...")
    init_db()
    log.info("Database ready.")

    state = load_state()
    log.info(f"State loaded: battery={state['current_wh']:.1f}Wh, pc_shut_down={state['pc_is_shut_down']}")

    log.info("Creating Tapo API client...")
    client = ApiClient(TAPO_EMAIL, TAPO_PASS, timeout_s=TAPO_TIMEOUT)
    log.info("Tapo client created. Entering main loop.")

    while True:
        try:
            # Try to contact the Smart Plug (timeout handled by ApiClient)
            log.info(f"Polling Tapo plug at {TAPO_IP}...")
            device = await client.p110(TAPO_IP)
            device_info = await device.get_device_info()
            energy_usage = await device.get_energy_usage()

            # current_power is in milliwatts, may be None on some models
            current_power_mw = getattr(energy_usage, 'current_power', None)
            current_watts = (current_power_mw / 1000) if current_power_mw is not None else 0.0
            uptime = device_info.on_time
            log.info(f"Plug OK — power={current_watts:.1f}W, uptime={uptime}s")
            
            # 1. POWER IS ON (Grid Restored / Charging)
            if state["power_cut_time"] is not None:
                # Power just came back!
                cut_duration = int(time.time() - state["power_cut_time"]) / 60
                
                # Check for Wi-Fi Blip False Positive
                if uptime < 300: # Plug rebooted recently, so it was a real power cut
                    log_event("POWER RESTORED", f"Duration: {round(cut_duration)} mins")
                    
                    # Do we need to wake the PC?
                    if state["pc_is_shut_down"]:
                        send_magic_packet(PC_MAC)
                        log_event("WOL PACKET SENT", f"Waking 3090 Rig ({PC_MAC})")
                        state["pc_is_shut_down"] = False
                else:
                    log_event("WIFI BLIP DETECTED", "Plug never lost power. Ignoring.")
                
                state["power_cut_time"] = None
            
            # Charge the Virtual Battery
            state["current_wh"] = min(state["current_wh"] + CHARGE_WH_PER_MIN, USABLE_CAPACITY_WH)
            battery_pct = (state["current_wh"] / USABLE_CAPACITY_WH) * 100
            
            log_telemetry(current_watts, True, battery_pct)
            save_state(state)

        except asyncio.TimeoutError:
            log.warning(f"Tapo plug at {TAPO_IP} unreachable (timeout after {TAPO_TIMEOUT}s)")
            # 2. POWER IS OUT (Connection Timeout)
            if state["power_cut_time"] is None:
                state["power_cut_time"] = time.time()
                log_event("POWER CUT DETECTED", "Grid failure.")
            
            outage_duration_mins = (time.time() - state["power_cut_time"]) / 60
            
            # Drain the Virtual Battery
            state["current_wh"] = max(state["current_wh"] - DISCHARGE_WH_PER_MIN, 0)
            battery_pct = (state["current_wh"] / USABLE_CAPACITY_WH) * 100
            
            log_telemetry(0.0, False, battery_pct)
            
            # 3. THE FAILSAFE (Shutdown PC if limit reached)
            if outage_duration_mins >= MAX_OUTAGE_MINUTES and not state["pc_is_shut_down"]:
                log_event("SHUTDOWN INITIATED", "4-Hour Limit Reached. Saving 3090 Rig.")
                
                # Execute SSH Shutdown (Requires passwordless SSH keys setup)
                subprocess.run(["ssh", f"{PC_USER}@{PC_IP}", SHUTDOWN_CMD], capture_output=True)
                
                state["pc_is_shut_down"] = True
            
            save_state(state)

        except Exception as e:
            log.error(f"Unexpected error: {type(e).__name__}: {e}")
            # Still treat as power out — same drain/shutdown logic
            if state["power_cut_time"] is None:
                state["power_cut_time"] = time.time()
                log_event("POWER CUT DETECTED", f"Grid failure ({type(e).__name__}).")

            outage_duration_mins = (time.time() - state["power_cut_time"]) / 60

            state["current_wh"] = max(state["current_wh"] - DISCHARGE_WH_PER_MIN, 0)
            battery_pct = (state["current_wh"] / USABLE_CAPACITY_WH) * 100

            log_telemetry(0.0, False, battery_pct)

            if outage_duration_mins >= MAX_OUTAGE_MINUTES and not state["pc_is_shut_down"]:
                log_event("SHUTDOWN INITIATED", "4-Hour Limit Reached. Saving 3090 Rig.")
                subprocess.run(["ssh", f"{PC_USER}@{PC_IP}", SHUTDOWN_CMD], capture_output=True)
                state["pc_is_shut_down"] = True

            save_state(state)

        # Sleep for exactly 60 seconds
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())