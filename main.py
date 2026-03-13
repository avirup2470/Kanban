import firebase_admin
from firebase_admin import credentials, firestore
import json
import time
from datetime import datetime
import os
import threading
import socket
from evdev import InputDevice, list_devices, categorize, ecodes

# --- CONFIGURATION ---
SERVICE_ACCOUNT_PATH = '/home/pi/kanban_project/firebase_config.json'
FIXED_LOCATION = "IM" 
LED_PIN = 14
SCANNER_NAME = "ARM CM0 USB HID Keyboard" 

# Global state for status monitoring
is_processing = False
internet_connected = False
firebase_ready = False

# Initialize Firebase
try:
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        raise FileNotFoundError(f"Config file missing at {SERVICE_ACCOUNT_PATH}")
    with open(SERVICE_ACCOUNT_PATH, 'r', encoding='utf-8') as f:
        config_data = json.load(f)
    cred = credentials.Certificate(config_data)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    firebase_ready = True
    print(f"--- System Ready: Location {FIXED_LOCATION} ---")
except Exception as e:
    print(f"Firebase Init Failed: {e}")
    firebase_ready = False

# Initialize GPIO
try:
    import lgpio
    h = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_output(h, LED_PIN)
except Exception:
    h = None

def blink_led(times, speed=0.1):
    """Utility for immediate feedback blinks (Success/Error)."""
    if h is None: return
    for _ in range(times):
        lgpio.gpio_write(h, LED_PIN, 1)
        time.sleep(speed)
        lgpio.gpio_write(h, LED_PIN, 0)
        time.sleep(speed)

def status_monitor_thread():
    """Background thread to check connectivity and manage status LEDs."""
    global internet_connected, firebase_ready, is_processing
    
    while True:
        # 1. Check Internet Connectivity (Ping Google DNS)
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3)
            internet_connected = True
        except OSError:
            internet_connected = False

        # 2. Manage LED Priority Logic
        if is_processing:
            # Processing: Blink 5 sec interval (On for 2.5s, Off for 2.5s)
            if h: lgpio.gpio_write(h, LED_PIN, 1)
            time.sleep(2.5)
            if h: lgpio.gpio_write(h, LED_PIN, 0)
            time.sleep(2.5)
            
        elif not internet_connected:
            # No Internet: Blink 2 times in 1 second
            for _ in range(2):
                if h: lgpio.gpio_write(h, LED_PIN, 1)
                time.sleep(0.2)
                if h: lgpio.gpio_write(h, LED_PIN, 0)
                time.sleep(0.3)
                
        elif not firebase_ready:
            # No Firebase: Blink 2 sec interval
            if h: lgpio.gpio_write(h, LED_PIN, 1)
            time.sleep(1.0)
            if h: lgpio.gpio_write(h, LED_PIN, 0)
            time.sleep(1.0)
            
        else:
            # Idle & Connected: Keep LED Off or very dim pulse
            time.sleep(1.0)

def db_upload_thread(event, clean_data):
    """Handles the actual Firestore communication in a background thread."""
    global is_processing
    is_processing = True
    try:
        part_id = event.get('Parts document ID')
        card_id = str(event.get('Id'))
        arrival_loc = event.get('ArrivalLocation')
        qty = float(event.get('Qt', 0))

        if not all([part_id, card_id, arrival_loc]):
            print("ERROR: Missing fields in JSON")
            return

        is_production = (FIXED_LOCATION == arrival_loc)
        card_ref = db.collection('Cards').document(card_id)
        
        card_snap = card_ref.get()

        if is_production:
            if card_snap.exists and card_snap.to_dict().get('Activation') == True:
                print(f"BLOCKED: Card {card_id} is already ACTIVE.")
                blink_led(1, 1.0)
                return
            signed_qt = qty
        else:
            if not card_snap.exists or card_snap.to_dict().get('Activation') == False:
                print(f"BLOCKED: Card {card_id} is already INACTIVE.")
                blink_led(1, 1.0)
                return
            signed_qt = -qty

        batch = db.batch()
        
        event_ref = (db.collection('Locations').document(arrival_loc)
                     .collection('Parts').document(part_id)
                     .collection('Events').document())
        batch.set(event_ref, {
            **event, 
            'Qt': signed_qt, 
            'SourceLoggingLocation': FIXED_LOCATION, 
            'Time': firestore.SERVER_TIMESTAMP
        })
        
        part_ref = (db.collection('Locations').document(arrival_loc)
                    .collection('Parts').document(part_id))
        batch.set(part_ref, {
            'Name': part_id, 
            'QT': firestore.Increment(signed_qt), 
            'LastUpdated': firestore.SERVER_TIMESTAMP
        }, merge=True)
        
        batch.set(card_ref, {
            'Activation': is_production, 
            'Part': part_id, 
            'LastActivated': firestore.SERVER_TIMESTAMP
        }, merge=True)

        batch.commit()
        print(f"SUCCESS: {card_id} uploaded to Firebase.")
        is_processing = False # End pulse before success blink
        blink_led(3, 0.05)
    except Exception as e:
        print(f"DATABASE ERROR: {e}")
        is_processing = False
        blink_led(1, 1.0)
    finally:
        is_processing = False

def process_scan(raw_data):
    clean_data = raw_data.strip()
    if not clean_data:
        return
    
    print(f"\nProcessing Raw Data: {clean_data}")

    try:
        event = json.loads(clean_data)
        t = threading.Thread(target=db_upload_thread, args=(event, clean_data))
        t.daemon = True
        t.start()
        print("Upload started in background...")
    except Exception as e:
        print(f"PARSING ERROR: {e}")
        blink_led(1, 1.0)

# --- EVDEV KEYBOARD LISTENER ---

def run_listener():
    devices = [InputDevice(path) for path in list_devices()]
    scanner = None
    for device in devices:
        if SCANNER_NAME.lower() in device.name.lower():
            scanner = device
            break
            
    if not scanner:
        print(f"Scanner '{SCANNER_NAME}' not found.")
        return

    print(f"Listening to: {scanner.name}")
    
    # Start the status monitor thread
    monitor = threading.Thread(target=status_monitor_thread)
    monitor.daemon = True
    monitor.start()
    
    key_map = {
        'KEY_1': '1', 'KEY_2': '2', 'KEY_3': '3', 'KEY_4': '4', 'KEY_5': '5',
        'KEY_6': '6', 'KEY_7': '7', 'KEY_8': '8', 'KEY_9': '9', 'KEY_0': '0',
        'KEY_Q': 'q', 'KEY_W': 'w', 'KEY_E': 'e', 'KEY_R': 'r', 'KEY_T': 't',
        'KEY_Y': 'y', 'KEY_U': 'u', 'KEY_I': 'i', 'KEY_O': 'o', 'KEY_P': 'p',
        'KEY_A': 'a', 'KEY_S': 's', 'KEY_D': 'd', 'KEY_F': 'f', 'KEY_G': 'g',
        'KEY_H': 'h', 'KEY_J': 'j', 'KEY_K': 'k', 'KEY_L': 'l', 'KEY_Z': 'z',
        'KEY_X': 'x', 'KEY_C': 'c', 'KEY_V': 'v', 'KEY_B': 'b', 'KEY_N': 'n',
        'KEY_M': 'm', 'KEY_SPACE': ' ', 'KEY_LEFTBRACE': '[', 'KEY_RIGHTBRACE': ']',
        'KEY_BACKSLASH': '\\', 'KEY_COMMA': ',', 'KEY_DOT': '.', 'KEY_SLASH': '/',
        'KEY_SEMICOLON': ';', 'KEY_APOSTROPHE': "'", 'KEY_MINUS': '-', 'KEY_EQUAL': '=',
        'KEY_GRAVE': '`', 'KEY_KP0': '0', 'KEY_KP1': '1', 'KEY_KP2': '2', 'KEY_KP3': '3',
        'KEY_KP4': '4', 'KEY_KP5': '5', 'KEY_KP6': '6', 'KEY_KP7': '7', 'KEY_KP8': '8', 'KEY_KP9': '9'
    }

    shift_map = {
        '1': '!', '2': '@', '3': '#', '4': '$', '5': '%', '6': '^', '7': '&', '8': '*', '9': '(', '0': ')',
        ';': ':', "'": '"', ',': '<', '.': '>', '/': '?', '[': '{', ']': '}', '\\': '|', '-': '_', '=': '+'
    }

    buffer = ""
    shift_active = False

    try:
        for event in scanner.read_loop():
            if event.type == ecodes.EV_KEY:
                key_event = categorize(event)
                if key_event.keystate == key_event.key_down:
                    if key_event.keycode in ['KEY_LEFTSHIFT', 'KEY_RIGHTSHIFT']:
                        shift_active = True
                        continue
                    
                    if key_event.keycode == 'KEY_ENTER':
                        process_scan(buffer)
                        buffer = ""
                        continue
                        
                    char = key_map.get(str(key_event.keycode), "")
                    if shift_active and char in shift_map:
                        char = shift_map[char]
                    elif shift_active:
                        char = char.upper()
                        
                    buffer += char
                    print(char, end="", flush=True)
                    
                elif key_event.keystate == key_event.key_up:
                    if key_event.keycode in ['KEY_LEFTSHIFT', 'KEY_RIGHTSHIFT']:
                        shift_active = False
    except KeyboardInterrupt:
        print("\nExiting...")

if __name__ == "__main__":
    run_listener()
