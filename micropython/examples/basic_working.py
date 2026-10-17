"""Basic KWP2000 MicroPython example.

Mirrors examples/basic_working/basic_working.ino: connect, read sensors,
read/clear trouble codes, print status and keep the connection alive.

Copy this file and kwp2000.py to the board, adjust the configuration below,
then run it. Type the single-character commands in the REPL.

Tested defaults are for an ESP32 (UART2, TX=GPIO17, RX=GPIO16). For a
Raspberry Pi Pico use something like UART_ID=1, TX_PIN=4, RX_PIN=5, and so on.
"""

import sys
import time

try:
    import select
except ImportError:
    import uselect as select

from machine import UART

from kwp2000 import KWP2000

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BRAND = "suzuki"          # "suzuki" or "kawasaki"
UART_ID = 2               # ESP32 UART2; adjust for your board
TX_PIN = 17               # K-line interface TX (also driven during fast init)
RX_PIN = 16               # K-line interface RX
DEALER_PIN = None         # e.g. 4 for Suzuki dealer mode
DEBUG_LEVEL = KWP2000.DEBUG_VERBOSE

HELP = (
    "Commands: i=connect, d=dealer mode, t=trouble codes, "
    "s=sensors, p=status, c=disconnect, ?=help"
)

uart = UART(UART_ID, baudrate=10400, tx=TX_PIN, rx=RX_PIN)
ecu = KWP2000(uart, BRAND, tx_pin=TX_PIN, rx_pin=RX_PIN)
ecu.enable_debug(level=DEBUG_LEVEL)

if DEALER_PIN is not None:
    if not ecu.enable_dealer_mode(DEALER_PIN):
        print("Dealer mode is available only on Suzuki")


# ---------------------------------------------------------------------------
# Non-blocking REPL input
# ---------------------------------------------------------------------------

poller = None
try:
    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)
except Exception as exc:
    print("Interactive commands unavailable:", exc)
    print("keep_alive() will still run; reset the board to stop.")


def read_key():
    if poller is None:
        return None
    try:
        if poller.poll(0):
            return sys.stdin.read(1)
    except Exception:
        return None
    return None


def handle_key(key):
    if key == "i":
        if ecu.connect():
            print("Connected")
        else:
            print("Connection failed:", ecu.errors)

    elif key == "d":
        mode = ecu.dealer_mode
        if mode is None:
            print("Dealer mode not enabled (set DEALER_PIN)")
        else:
            ecu.set_dealer_mode(not mode)
            print("Dealer mode:", ecu.dealer_mode)

    elif key == "t":
        codes = ecu.read_trouble_codes()
        if codes:
            for code, status in codes:
                if status is None:
                    print("DTC 0x%04X" % code)
                else:
                    print("DTC 0x%04X status 0x%02X" % (code, status))
        else:
            print("No trouble codes")

    elif key == "s":
        if ecu.request_sensors():
            print(ecu.sensors)
            ecu.print_sensors()
        else:
            print("Sensor request failed:", ecu.errors)

    elif key == "p":
        ecu.print_status(min_interval_ms=0)

    elif key == "c":
        if ecu.disconnect():
            print("Disconnected")
        else:
            print("Stop request failed:", ecu.errors)

    elif key == "?":
        print(HELP)

    elif key in ("\r", "\n"):
        pass

    else:
        print("Unknown command: %r (press ? for help)" % key)


print("KWP2000 MicroPython example")
print(HELP)

while True:
    key = read_key()
    if key:
        handle_key(key)
    ecu.keep_alive()
    time.sleep_ms(10)
