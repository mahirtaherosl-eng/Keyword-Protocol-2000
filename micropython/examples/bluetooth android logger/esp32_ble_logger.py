"""ESP32-C6 KWP2000 BLE sensor logger.

Polls all ECU sensor values with the KWP2000 MicroPython library and streams
them over BLE as newline-delimited JSON. An Android app (or the optional PC
receiver in ``tools/ble_receiver.py``) connects over GATT and can change the
sensor polling interval while logging.

Wiring
------
ESP32-C6 defaults: UART1 TX=GPIO19 -> L9637 TX input (pin 4),
RX=GPIO20 <- L9637 RX output (pin 1). L9637 VCC (pin 3) is powered from
GPIO22 (high while the program runs, low on exit); VS (pin 7) goes to 12 V.
Use a common ground with the bike. Never connect an ESP32 pin directly to the
12 V K-line. UART0 (GPIO16/17) is reserved for the REPL on the generic C6
firmware; GPIO10/11 are not usable on this board.

BLE interface
-------------
Service    6b773230-2d4b-5750-8000-6b7770323030
Sensor     6b773230-2d4b-5750-8001-6b7770323030  NOTIFY (JSON lines)
Control    6b773230-2d4b-5750-8002-6b7770323030  WRITE  ({"cmd":"interval","ms":2000})

Each notification is a chunk of a newline-delimited JSON line. The central
must buffer chunks and split on "\\n".

Run
---
Copy kwp2000.py to :lib/ and this file to the board, then run it or save it
as :main.py. The ESP32 advertises as "KWP2000-C6". Set SIMULATE = True to
test the BLE/app path without an ECU.

Blocking budget (no watchdog is enabled):
  request_sensors() can block ~6.5 s (Suzuki) or ~44-45 s (Kawasaki) on a
  silent bus; connect() up to ~7.5 s; disconnect() up to ~8.5 s.
"""

import time

import bluetooth

from machine import Pin, UART

from kwp2000 import KWP2000

try:
    import json
except ImportError:          # some builds only expose ujson
    import ujson as json


# ---------------------------------------------------------------------------
# Configuration - edit for your bike
# ---------------------------------------------------------------------------

BRAND = "kawasaki"            # "kawasaki" or "suzuki"
DEVICE_NAME = "KWP2000-C6"

# ESP32-C6 board profile (the WiFi dashboard example has alternates).
UART_ID = 1
TX_PIN = 19                   # UART TX and fast-init GPIO
RX_PIN = 20                   # UART RX
L9637_VCC_PIN = 22
L9637_VCC_SETTLE_MS = 100

POLL_INTERVAL_MS = 2000       # default sensor poll period
MIN_POLL_INTERVAL_MS = 1000
MAX_POLL_INTERVAL_MS = 10000
KEEPALIVE_INTERVAL_MS = 1000  # must stay <= 2000
LOOP_SLEEP_MS = 20
RECONNECT_MIN_MS = 2000
RECONNECT_MAX_MS = 60000

ADV_INTERVAL_US = 500000
PREFERRED_MTU = 247
SIMULATE = False              # True: synthetic sensors, no UART/ECU
DEBUG_LEVEL = KWP2000.DEBUG_NONE   # DEBUG_DEFAULT during bring-up

SERVICE_UUID = "6b773230-2d4b-5750-8000-6b7770323030"
SENSOR_UUID = "6b773230-2d4b-5750-8001-6b7770323030"
CONTROL_UUID = "6b773230-2d4b-5750-8002-6b7770323030"

SENSOR_KEYS = ("gps", "clutch", "rpm", "speed", "tps",
               "stps", "iap", "iat", "ect", "voltage")

# BLE event ids are not exported by the bluetooth module.
_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3
_IRQ_MTU_EXCHANGED = 21


# ---------------------------------------------------------------------------
# Pure helpers (importable by tools/test_protocol.py)
# ---------------------------------------------------------------------------

def _ad_element(adv_type, value):
    """Encode one BLE advertising data element."""
    return bytes((len(value) + 1, adv_type)) + value


def adv_payload(service_uuid_bytes):
    """Flags plus the complete 128-bit service UUID (max 31 bytes)."""
    payload = (
        _ad_element(0x01, b"\x06")        # general discoverable, no BR/EDR
        + _ad_element(0x07, service_uuid_bytes)
    )
    if len(payload) > 31:
        raise ValueError("advertising payload too large")
    return payload


def scan_response(name):
    """Complete local name for the scan response (max 31 bytes)."""
    payload = _ad_element(0x09, name.encode())
    if len(payload) > 31:
        raise ValueError("scan response too large")
    return payload


def chunk_data(data, mtu):
    """Split a notification payload into MTU-sized chunks."""
    size = max(20, mtu - 3)
    return [data[i:i + size] for i in range(0, len(data), size)]


def _rounded(value):
    if isinstance(value, float):
        return round(value, 2)
    return value


def build_sample(ecu, seq):
    """Return one newline-terminated JSON sample as bytes."""
    sensors = ecu.sensors
    obj = {"seq": seq, "ms": time.ticks_ms()}
    for key in SENSOR_KEYS:
        obj[key] = _rounded(sensors.get(key))
    return (json.dumps(obj) + "\n").encode()


def handle_control(raw, current_interval):
    """Apply one control command and return (interval_ms, ack_dict)."""
    try:
        text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        command = json.loads(text.strip())
    except (ValueError, TypeError):
        return current_interval, {
            "ack": "interval", "ok": False, "error": "invalid json",
        }

    if not isinstance(command, dict) or command.get("cmd") != "interval":
        return current_interval, {
            "ack": "interval", "ok": False, "error": "unknown command",
        }

    try:
        value = int(command.get("ms"))
    except (TypeError, ValueError):
        return current_interval, {
            "ack": "interval", "ok": False, "error": "invalid ms",
        }

    value = max(MIN_POLL_INTERVAL_MS, min(MAX_POLL_INTERVAL_MS, value))
    return value, {"ack": "interval", "ms": value, "ok": True}


# ---------------------------------------------------------------------------
# BLE GATT server
# ---------------------------------------------------------------------------

class BleStream:
    """Advertise the sensor service and send notifications."""

    def __init__(self, name=DEVICE_NAME, preferred_mtu=PREFERRED_MTU):
        self.ble = bluetooth.BLE()
        self.ble.active(True)
        try:
            self.ble.config(gap_name=name)
        except (TypeError, ValueError):
            # Some firmware expects bytes rather than str.
            self.ble.config(gap_name=name.encode())
        try:
            # Required to accept an MTU larger than 23; ignore older ports.
            self.ble.config(mtu=preferred_mtu)
        except (TypeError, ValueError, OSError):
            pass

        self.conn_handle = None
        self.mtu = 23
        self.advertising = False
        self.pending_command = None

        service = (
            bluetooth.UUID(SERVICE_UUID),
            (
                (bluetooth.UUID(SENSOR_UUID), bluetooth.FLAG_NOTIFY),
                (bluetooth.UUID(CONTROL_UUID), bluetooth.FLAG_WRITE),
            ),
        )
        ((self.data_handle, self.control_handle),) = (
            self.ble.gatts_register_services((service,))
        )
        self._adv_data = adv_payload(bytes(bluetooth.UUID(SERVICE_UUID)))
        self._resp_data = scan_response(name)

        self.ble.irq(self._irq)
        self._try_advertise()

    @property
    def connected(self):
        return self.conn_handle is not None

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            conn_handle = data[0] if isinstance(data, tuple) else data
            if self.conn_handle is not None:
                # Accept one central; reject a second connection.
                try:
                    self.ble.gap_disconnect(conn_handle)
                except OSError:
                    pass
                return
            self.conn_handle = conn_handle
            self.mtu = 23
            self.advertising = False
            try:
                self.ble.gap_advertise(None)
            except OSError:
                pass

        elif event == _IRQ_CENTRAL_DISCONNECT:
            conn_handle = data[0] if isinstance(data, tuple) else data
            if conn_handle == self.conn_handle:
                self.conn_handle = None
                self.mtu = 23
                self._try_advertise()

        elif event == _IRQ_GATTS_WRITE:
            attr_handle = data[1]
            if attr_handle == self.control_handle:
                try:
                    self.pending_command = self.ble.gatts_read(attr_handle)
                except OSError:
                    pass

        elif event == _IRQ_MTU_EXCHANGED:
            conn_handle, mtu = data[0], data[1]
            if conn_handle == self.conn_handle:
                self.mtu = mtu

    def _try_advertise(self):
        if self.conn_handle is not None:
            return
        try:
            self.ble.gap_advertise(
                ADV_INTERVAL_US,
                adv_data=self._adv_data,
                resp_data=self._resp_data,
            )
            self.advertising = True
        except (OSError, ValueError):
            self.advertising = False

    def send(self, line):
        """Send one newline-terminated line, chunked for the current MTU."""
        conn_handle = self.conn_handle
        if conn_handle is None:
            return False
        for chunk in chunk_data(line, self.mtu):
            for attempt in range(4):
                try:
                    self.ble.gatts_notify(
                        conn_handle, self.data_handle, chunk
                    )
                    break
                except OSError:
                    if attempt == 3:
                        return False
                    time.sleep_ms(10)
            time.sleep_ms(2)
        return True

    def close(self):
        try:
            self.ble.gap_advertise(None)
        except OSError:
            pass
        try:
            self.ble.active(False)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Synthetic ECU for bench testing
# ---------------------------------------------------------------------------

class FakeEcu:
    """Plausible changing values so the BLE/app path can be tested."""

    def __init__(self):
        self.connected = False
        self._sensors = {key: None for key in SENSOR_KEYS}

    def connect(self):
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False
        return True

    def keep_alive(self, interval_ms=0):
        return self.connected

    def request_sensors(self):
        if not self.connected:
            return False
        t = time.ticks_ms() // 1000
        self._sensors = {
            "gps": (t % 6) + 1,
            "clutch": t % 2,
            "rpm": 900 + (t * 137) % 6000,
            "speed": (t * 7) % 180,
            "tps": 5 + (t * 3) % 80,
            "stps": 50.0,
            "iap": 95.0 + (t % 20),
            "iat": 25.0,
            "ect": 78.0 + (t % 12),
            "voltage": 14.1,
        }
        return True

    @property
    def sensors(self):
        return dict(self._sensors)


# ---------------------------------------------------------------------------
# ECU setup and main loop
# ---------------------------------------------------------------------------

def _log_errors(prefix, ecu):
    print("%s: %s" % (prefix, getattr(ecu, "errors", ())))


def create_ecu():
    """Create the K-line UART and the KWP2000 instance."""
    try:
        uart = UART(UART_ID, baudrate=10400, tx=TX_PIN, rx=RX_PIN)
    except OSError as exc:
        print("UART%d init failed on TX=%s RX=%s: %s" %
              (UART_ID, TX_PIN, RX_PIN, exc))
        print("ESP32-C6: use UART_ID=1, TX_PIN=19, RX_PIN=20.")
        print("  UART0/GPIO16-17 is the REPL; GPIO10/11 do not work here.")
        print("  L9637 VCC is switched from GPIO22; check the module wiring.")
        raise

    ecu = KWP2000(uart, BRAND, tx_pin=TX_PIN, rx_pin=RX_PIN,
                  uart_id=UART_ID)
    if DEBUG_LEVEL:
        ecu.enable_debug(level=DEBUG_LEVEL)
    return ecu


def run(ecu, ble_stream):
    """Poll the ECU while a central is connected; keep it alive otherwise."""
    poll_interval_ms = POLL_INTERVAL_MS
    next_connect = time.ticks_ms()
    next_poll = time.ticks_ms()
    backoff = RECONNECT_MIN_MS
    seq = 0

    while True:
        command = ble_stream.pending_command
        if command is not None:
            ble_stream.pending_command = None
            poll_interval_ms, ack = handle_control(command, poll_interval_ms)
            next_poll = time.ticks_add(time.ticks_ms(), poll_interval_ms)
            ble_stream.send((json.dumps(ack) + "\n").encode())
            print("control:", ack)

        if not ble_stream.advertising and not ble_stream.connected:
            ble_stream._try_advertise()

        now = time.ticks_ms()

        if not ble_stream.connected:
            # No phone: keep the ECU session alive, but do not poll sensors.
            if ecu.connected:
                if not ecu.keep_alive(KEEPALIVE_INTERVAL_MS):
                    _log_errors("link lost", ecu)
                    next_connect = time.ticks_add(
                        time.ticks_ms(), backoff
                    )
                    backoff = min(backoff * 2, RECONNECT_MAX_MS)
            time.sleep_ms(LOOP_SLEEP_MS)
            continue

        if not ecu.connected:
            if time.ticks_diff(now, next_connect) >= 0:
                print("Connecting to ECU...")
                if ecu.connect():
                    print("ECU connected")
                    backoff = RECONNECT_MIN_MS
                    next_poll = time.ticks_ms()
                else:
                    _log_errors("connect failed", ecu)
                    next_connect = time.ticks_add(
                        time.ticks_ms(), backoff
                    )
                    backoff = min(backoff * 2, RECONNECT_MAX_MS)
            time.sleep_ms(LOOP_SLEEP_MS)
            continue

        if not ecu.keep_alive(KEEPALIVE_INTERVAL_MS):
            _log_errors("link lost", ecu)
            next_connect = time.ticks_add(time.ticks_ms(), backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_MS)
            time.sleep_ms(LOOP_SLEEP_MS)
            continue

        if time.ticks_diff(time.ticks_ms(), next_poll) >= 0:
            started = time.ticks_ms()
            if ecu.request_sensors():
                seq += 1
                ble_stream.send(build_sample(ecu, seq))
            else:
                _log_errors("sensor poll failed", ecu)

            next_poll = time.ticks_add(started, poll_interval_ms)
            if time.ticks_diff(time.ticks_ms(), next_poll) >= 0:
                # The poll overran its slot; wait a full interval.
                next_poll = time.ticks_add(
                    time.ticks_ms(), poll_interval_ms
                )

            # A long failed poll may have passed the 2 s link expiry.
            if not ecu.keep_alive(KEEPALIVE_INTERVAL_MS):
                _log_errors("link lost after poll", ecu)
                next_connect = time.ticks_add(
                    time.ticks_ms(), backoff
                )
                backoff = min(backoff * 2, RECONNECT_MAX_MS)

        time.sleep_ms(LOOP_SLEEP_MS)


def main():
    """Power the L9637, start BLE, and run until interrupted."""
    vcc = None
    ble_stream = None
    ecu = None

    if not SIMULATE:
        vcc = Pin(L9637_VCC_PIN, Pin.OUT, value=0)
        vcc.value(1)
        time.sleep_ms(L9637_VCC_SETTLE_MS)

    try:
        try:
            ble_stream = BleStream()
        except Exception as exc:
            print("BLE init failed:", exc)
            print("Use an official ESP32_GENERIC_C6 build that includes "
                  "the bluetooth module.")
            raise
        print("BLE advertising as", DEVICE_NAME)

        if SIMULATE:
            ecu = FakeEcu()
            ecu.connect()
            print("SIMULATE mode: no UART or ECU access")
        else:
            ecu = create_ecu()

        run(ecu, ble_stream)
    finally:
        if ecu is not None:
            print("Disconnecting ECU (up to ~8.5 s)...")
            try:
                ecu.disconnect()
            except Exception:
                pass
        if ble_stream is not None:
            ble_stream.close()
        if vcc is not None:
            vcc.value(0)
        print("Stopped")


if __name__ == "__main__":
    main()
