# SPDX-License-Identifier: GPL-3.0-or-later
"""
kwp2000.py - MicroPython port of the KWP2000 Arduino library.

This module talks KWP2000 / ISO 14230 to Suzuki and Kawasaki motorbike ECUs
over a K-line. It is a Python port of the Arduino library in this repository.

Original C++ library: Copyright (c) Aster94.
The original C++ source files carry an MIT-style permission notice; this port is
distributed under the repository's GPL-3.0 license. See the LICENSE file.

Typical use::

    from machine import UART
    from kwp2000 import KWP2000

    uart = UART(2, baudrate=10400, tx=17, rx=16)
    ecu = KWP2000(uart, "suzuki", tx_pin=17, rx_pin=16)

    if ecu.connect():
        ecu.request_sensors()
        print(ecu.rpm, ecu.speed, ecu.ect)
        ecu.disconnect()
"""

import sys
import time

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# Time compatibility helpers
#
# MicroPython provides ticks_ms/ticks_diff/sleep_ms. CPython does not, so the
# host test suite uses these fallbacks. All internal code calls the module-level
# names below, which lets tests patch them with a fake clock.
# ---------------------------------------------------------------------------

try:
    ticks_ms = time.ticks_ms
except AttributeError:
    try:
        _monotonic = time.monotonic
    except AttributeError:
        _monotonic = time.time

    def ticks_ms():
        return int(_monotonic() * 1000)


try:
    ticks_diff = time.ticks_diff
except AttributeError:
    def ticks_diff(a, b):
        return a - b


try:
    sleep_ms = time.sleep_ms
except AttributeError:
    def sleep_ms(ms):
        if ms > 0:
            time.sleep(ms / 1000.0)


# ---------------------------------------------------------------------------
# Protocol constants (ISO 14230 / original ISO.h)
# ---------------------------------------------------------------------------

ISO_BAUDRATE = 10400
ISO_MAX_DATA = 260
ISO_T_IDLE = 1000
ISO_T_INIL = 25
ISO_T_WUP = 25
ISO_T_P2_MIN = 25
ISO_T_P3_MIN = 55
ISO_T_P3_MAX = 2000
ISO_T_P4_MIN = 10
KEEP_ALIVE_MS = 1500
OUR_ADDR = 0xF1

FORMAT_PHYSICAL = 0x80
FORMAT_FUNCTIONAL = 0xC0
FORMAT_CARB = 0x40

START_COM = b"\x81"
STOP_COM = b"\x82"
START_DIAGNOSTIC = b"\x10\x80"
TESTER_PRESENT = b"\x3E\x01"
REJECTED = 0x7F

# Error bits, compatible with the C++ error_enum.
ERR_TEST = 1 << 0
ERR_START = 1 << 1
ERR_STOP = 1 << 2
ERR_TO = 1 << 3
ERR_FROM = 1 << 4
ERR_CHECKSUM = 1 << 5
ERR_ECHO = 1 << 6
ERR_UNEXPECTED = 1 << 7
ERR_HEADER = 1 << 8
ERR_USER = 1 << 9
ERR_CONFIG = 1 << 10
ERR_TIMEOUT = 1 << 11
ERR_RESPONSE = 1 << 12
ERR_TIMING = 1 << 13
ERR_WRONG_REJECT = 1 << 14
ERR_UNSUPPORTED = 1 << 15

_ERROR_NAMES = (
    (ERR_TEST, "test"),
    (ERR_START, "start"),
    (ERR_STOP, "stop"),
    (ERR_TO, "to"),
    (ERR_FROM, "from"),
    (ERR_CHECKSUM, "checksum"),
    (ERR_ECHO, "echo"),
    (ERR_UNEXPECTED, "unexpected"),
    (ERR_HEADER, "header"),
    (ERR_USER, "user"),
    (ERR_CONFIG, "config"),
    (ERR_TIMEOUT, "timeout"),
    (ERR_RESPONSE, "response"),
    (ERR_TIMING, "timing"),
    (ERR_WRONG_REJECT, "wrong_reject"),
    (ERR_UNSUPPORTED, "unsupported"),
)

# Brands
BRAND_SUZUKI = 0
BRAND_KAWASAKI = 1
BRAND_YAMAHA = 2
BRAND_HONDA = 3
_BRAND_NAMES = ("suzuki", "kawasaki", "yamaha", "honda")
_BRAND_CONFIG = {
    BRAND_SUZUKI: {"ecu_addr": 0x12},
    BRAND_KAWASAKI: {"ecu_addr": 0x11},
    BRAND_YAMAHA: {"ecu_addr": 0x3F},
    BRAND_HONDA: {"ecu_addr": 0x3F},
}

# Suzuki sensor byte offsets, measured from the start of the 2-byte
# service/local-identifier prefix at the beginning of the response payload.
# These reproduce the C++ absolute indices (26, 52, 17, ...) for the standard
# four-byte header while also working for shorter headers.
_SUZUKI_OFFSETS = {
    "gps": 20,
    "clutch": 46,
    "rpm_h": 11,
    "rpm_l": 12,
    "speed": 10,
    "tps": 13,
    "stps": 40,
    "iap": 14,
    "iat": 16,
    "ect": 15,
    "voltage": 18,
}

# Kawasaki sends one request per value.
_KAWASAKI_REQUESTS = (
    ("gps", b"\x21\x0B"),
    ("rpm", b"\x21\x09"),
    ("speed", b"\x21\x0C"),
    ("tps", b"\x21\x04"),
    ("iap", b"\x21\x07"),
    ("iat", b"\x21\x05"),
    ("ect", b"\x21\x06"),
)

_KMH_TO_MPH = 1.609344


# ---------------------------------------------------------------------------
# Pure protocol helpers
# ---------------------------------------------------------------------------

def _checksum(data):
    """Return the ISO 14230 8-bit sum checksum of *data*."""
    return sum(data) & 0xFF


def _hex(data):
    return " ".join(["%02X" % b for b in data])


def _tri_state(value):
    if value is None:
        return "auto"
    return "yes" if value else "no"


def _build_request(payload, ecu_addr, use_length_byte, use_addresses):
    """Build a tester request frame.

    Supports all four ISO 14230 header layouts. ``use_length_byte`` and
    ``use_addresses`` only affect requests; responses are parsed from their
    format byte.
    """
    n = len(payload)
    if n < 1 or n > 255:
        raise ValueError("payload length must be 1..255")

    embedded = (not use_length_byte) and n <= 0x3F
    frame = bytearray()
    frame.append((FORMAT_PHYSICAL | n) if embedded else FORMAT_PHYSICAL)
    if use_addresses:
        frame.append(ecu_addr & 0xFF)
        frame.append(OUR_ADDR)
    if not embedded:
        frame.append(n & 0xFF)
    frame.extend(payload)
    frame.append(_checksum(frame))
    return bytes(frame)


def _parse_key_bytes(low, high):
    """Decode the two ECU key bytes.

    Returns ``(use_length_byte, use_addresses, timing, errors)`` where the
    first two are True/False, "auto" (detect from the first response), or None
    (leave unchanged); timing is "normal", "extended", "auto", or None.
    """
    errors = 0
    if high != 0x8F:
        errors |= ERR_CONFIG

    al0 = low & 1
    al1 = (low >> 1) & 1
    hb0 = (low >> 2) & 1
    hb1 = (low >> 3) & 1
    tp0 = (low >> 4) & 1
    tp1 = (low >> 5) & 1

    if al1 and al0:
        use_length_byte = False
    elif al1 and not al0:
        use_length_byte = True
    elif al0:
        use_length_byte = False
    else:
        use_length_byte = None

    if hb1 and hb0:
        use_addresses = False
    elif hb1 and not hb0:
        use_addresses = True
    elif hb0:
        use_addresses = False
    else:
        use_addresses = None

    timing = None
    if tp1 and tp0:
        errors |= ERR_CONFIG
    elif tp1 and not tp0:
        timing = "normal"
    elif tp0:
        timing = "extended"
        errors |= ERR_UNSUPPORTED

    # All-zero AL/HB bits with TP0=1, TP1=0 means "copy the first response".
    if al0 == 0 and al1 == 0 and hb0 == 0 and hb1 == 0 and tp0 == 1 and tp1 == 0:
        use_length_byte = "auto"
        use_addresses = "auto"
        timing = "auto"

    if not ((low >> 6) & 1):
        errors |= ERR_CONFIG

    parity = 1
    for n in range(7):
        if (low >> n) & 1:
            parity ^= 1
    if ((low >> 7) & 1) != parity:
        errors |= ERR_CONFIG

    return use_length_byte, use_addresses, timing, errors


def _response_geometry(buf, use_addresses, our_addr=OUR_ADDR):
    """Work out the header and length of a response frame.

    Returns ``(header_len, data_len, total_len, addresses_used)`` or None if
    more bytes are needed. The response length style always comes from the
    format byte: a non-zero low-6-bit field means the length is embedded,
    zero means a separate length byte follows the addresses.
    """
    if not buf:
        return None

    b0 = buf[0]
    embedded = (b0 & 0x3F) != 0

    addresses = use_addresses
    if addresses is None:
        if len(buf) < 2:
            return None
        addresses = (buf[1] == our_addr)

    header_len = 1 + (2 if addresses else 0) + (0 if embedded else 1)
    if len(buf) < header_len:
        return None

    if embedded:
        data_len = b0 & 0x3F
    else:
        data_len = buf[header_len - 1]

    total = header_len + data_len + 1
    return header_len, data_len, total, addresses


def _parse_response(frame, header_len, addresses, ecu_addr, our_addr=OUR_ADDR):
    """Validate a complete response frame and return its payload."""
    errors = 0
    if (frame[0] & 0xC0) != FORMAT_PHYSICAL:
        errors |= ERR_HEADER

    if addresses:
        if frame[1] != our_addr:
            errors |= ERR_TO
        if frame[2] != ecu_addr:
            errors |= ERR_FROM

    checksum_ok = (_checksum(frame[:-1]) == frame[-1])
    if not checksum_ok:
        errors |= ERR_CHECKSUM

    payload = bytes(frame[header_len:-1])
    return payload, checksum_ok, errors


def _pin_init_output(pin, value):
    """Configure a machine.Pin-like object as an output.

    Falls back gracefully for test doubles and ports with a simpler Pin API.
    """
    try:
        from machine import Pin
        mode = Pin.OUT
    except ImportError:
        mode = None

    try:
        if mode is None:
            pin.init(value=value)
        else:
            pin.init(mode=mode, value=value)
    except (TypeError, AttributeError):
        if mode is not None:
            try:
                pin.init(mode=mode)
            except Exception:
                pass
        pin.value(value)


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class KWP2000:
    """KWP2000 / ISO 14230 client for Suzuki and Kawasaki motorbikes."""

    SUZUKI = BRAND_SUZUKI
    KAWASAKI = BRAND_KAWASAKI
    YAMAHA = BRAND_YAMAHA
    HONDA = BRAND_HONDA

    DEBUG_NONE = 0
    DEBUG_DEFAULT = 1
    DEBUG_VERBOSE = 2

    READ_TOTAL = 0
    READ_ONLY_ACTIVE = 1
    READ_ALL = 2

    def __init__(self, uart, brand, tx_pin=None, rx_pin=None, *,
                 uart_id=None, uart_kwargs=None, debug=None,
                 debug_level=DEBUG_NONE):
        """Create a KWP2000 client.

        :param uart: a machine.UART-like object (write/read/any/init), or None
            when ``uart_id`` is supplied.
        :param brand: ``KWP2000.SUZUKI`` / ``KAWASAKI`` / ``YAMAHA`` / ``HONDA``
            or a case-insensitive string such as ``"suzuki"``.
        :param tx_pin: int or machine.Pin used for the fast-init waveform. The
            UART is re-attached to it after the waveform.
        :param rx_pin: int or machine.Pin used when re-initializing the UART.
        :param uart_id: optional UART peripheral id. Required when ``uart`` is
            None; also used as a fallback if ``uart.init()`` cannot reattach.
        :param uart_kwargs: extra keyword arguments for ``uart.init()``.
        :param debug: stream with ``write()``; defaults to ``sys.stdout`` when
            debug is enabled.
        :param debug_level: one of ``DEBUG_NONE``, ``DEBUG_DEFAULT``,
            ``DEBUG_VERBOSE``.
        """
        if isinstance(brand, str):
            try:
                brand = _BRAND_NAMES.index(brand.lower())
            except ValueError:
                raise ValueError("unknown brand: %s" % brand)
        if brand not in (BRAND_SUZUKI, BRAND_KAWASAKI, BRAND_YAMAHA, BRAND_HONDA):
            raise ValueError("invalid brand: %r" % (brand,))
        if uart is None and uart_id is None:
            raise ValueError("either uart or uart_id must be supplied")

        self._uart = uart
        self._uart_id = uart_id
        self._uart_kwargs = {} if uart_kwargs is None else dict(uart_kwargs)
        self._brand = brand
        self._ecu_addr = _BRAND_CONFIG[brand]["ecu_addr"]
        self._tx_pin_arg = tx_pin
        self._rx_pin_arg = rx_pin
        self._tx_pin_obj = None if (tx_pin is None or isinstance(tx_pin, int)) else tx_pin

        self._dealer_pin = None
        self._dealer_enabled = False

        self._connected = False
        self._error_flags = 0

        # Request framing defaults, matching initKline() before the handshake.
        self._use_length_byte = False
        self._use_addresses = brand in (BRAND_SUZUKI, BRAND_KAWASAKI)
        self._timing = None

        self._response = b""
        self._response_payload = b""
        self._response_header_len = 0
        self._response_checksum_ok = False
        self._last_response_code = None
        self._last_nrc = None

        self._last_correct_response = 0
        self._connection_time = 0
        self._last_status_print = 0
        self._last_sensors = 0

        self._metric = True
        self._sensors = {
            "gps": None,
            "clutch": None,
            "rpm": None,
            "speed": None,
            "tps": None,
            "stps": None,
            "iap": None,
            "iat": None,
            "ect": None,
            "voltage": None,
        }

        self._debug_level = debug_level
        self._debug_enabled = debug_level > self.DEBUG_NONE
        if debug is not None:
            self._debug = debug
        elif self._debug_enabled:
            self._debug = sys.stdout
        else:
            self._debug = None

    # -- setup ------------------------------------------------------------

    def enable_debug(self, stream=None, level=DEBUG_DEFAULT):
        """Enable debug output on *stream* (default ``sys.stdout``)."""
        self._debug = sys.stdout if stream is None else stream
        self._debug_level = level
        self._debug_enabled = level > self.DEBUG_NONE

    def set_debug_level(self, level):
        """Change the debug level; level > DEBUG_NONE enables debug output."""
        self._debug_level = level
        self._debug_enabled = level > self.DEBUG_NONE
        if self._debug_enabled and self._debug is None:
            self._debug = sys.stdout

    def disable_debug(self):
        """Disable all debug output."""
        self._debug_enabled = False

    def enable_dealer_mode(self, pin):
        """Enable Suzuki dealer-mode control on *pin* (int or machine.Pin)."""
        if self._brand != BRAND_SUZUKI:
            self._error_flags |= ERR_USER
            return False

        if isinstance(pin, int):
            try:
                from machine import Pin
            except ImportError:
                raise RuntimeError("machine.Pin is required for dealer mode")
            pin = Pin(pin, Pin.OUT, value=0)
        else:
            _pin_init_output(pin, 0)

        self._dealer_pin = pin
        self._dealer_enabled = True
        return True

    def set_dealer_mode(self, enabled):
        """Drive the dealer-mode pin. Returns False if it was not enabled."""
        if not self._dealer_enabled or self._dealer_pin is None:
            self._error_flags |= ERR_USER
            return False
        self._dealer_pin.value(1 if enabled else 0)
        return True

    @property
    def dealer_mode(self):
        """Current dealer-mode pin state, or None if not enabled."""
        if not self._dealer_enabled or self._dealer_pin is None:
            return None
        return bool(self._dealer_pin.value())

    def use_imperial(self):
        """Display speed in mph and temperatures in degrees Fahrenheit."""
        self._metric = False

    def use_metric(self):
        """Display speed in km/h and temperatures in degrees Celsius."""
        self._metric = True

    # -- communication ----------------------------------------------------

    def connect(self):
        """Run the fast-init waveform and start KWP2000 communication.

        Blocks for roughly 1.05 seconds plus the handshake. Returns True on
        success or if already connected.
        """
        if self._connected:
            self._log(self.DEBUG_DEFAULT, "Already connected")
            return True

        self.reset_error()
        self._ensure_uart()
        if self._tx_pin_arg is None:
            raise RuntimeError("tx_pin is required for the fast-init waveform")

        self._release_uart()
        tx_pin = self._get_tx_pin()
        _pin_init_output(tx_pin, 1)

        self._log(self.DEBUG_DEFAULT, "Initialize K-line")
        sleep_ms(ISO_T_IDLE)
        tx_pin.value(0)
        sleep_ms(ISO_T_INIL)
        tx_pin.value(1)
        sleep_ms(ISO_T_WUP)

        self._use_length_byte = False
        self._use_addresses = self._brand in (BRAND_SUZUKI, BRAND_KAWASAKI)
        self._timing = None
        self._reinit_uart()
        self._drain_rx()

        if not self.handle_request(START_COM, attempts=1):
            self._error_flags |= ERR_START
            self._log(self.DEBUG_DEFAULT, "Initialization failed")
            return False

        payload = self._response_payload
        if len(payload) >= 3:
            use_lb, use_addr, timing, errors = _parse_key_bytes(payload[1], payload[2])
            if use_lb == "auto":
                self._use_length_byte = None
            elif use_lb is not None:
                self._use_length_byte = use_lb
            if use_addr == "auto":
                self._use_addresses = None
            elif use_addr is not None:
                self._use_addresses = use_addr
            if timing == "auto":
                self._timing = None
            elif timing is not None:
                self._timing = timing
            self._error_flags |= errors
            self._log(
                self.DEBUG_VERBOSE,
                "K line config: key bytes 0x%02X%02X, length byte=%s, "
                "addresses=%s, timing=%s" % (
                    payload[2], payload[1],
                    _tri_state(self._use_length_byte),
                    _tri_state(self._use_addresses),
                    "auto" if self._timing is None else self._timing,
                ),
            )
        else:
            self._error_flags |= ERR_CONFIG

        if self._brand == BRAND_KAWASAKI:
            if not self.handle_request(START_DIAGNOSTIC, attempts=3):
                self._error_flags |= ERR_START
                self._log(self.DEBUG_DEFAULT, "Failed to start diagnostic session")
                return False
            self._log(self.DEBUG_DEFAULT, "Start diagnostic successful")

        self._connected = True
        self._connection_time = ticks_ms()
        self._last_correct_response = ticks_ms()
        self._last_sensors = 0
        self._log(self.DEBUG_DEFAULT, "ECU connected")
        return True

    def disconnect(self):
        """Send stop communication, wait for P3 max, and mark disconnected.

        Blocks for up to about two seconds (plus retries). Always leaves the
        object disconnected; returns False if the stop request failed.
        """
        if not self._connected:
            return True

        ok = self.handle_request(STOP_COM, attempts=3)
        self._drain_rx()
        sleep_ms(ISO_T_P3_MAX)

        self._connected = False
        self._connection_time = 0
        self._last_correct_response = 0
        self._response = b""
        self._response_payload = b""
        self._response_header_len = 0
        self._response_checksum_ok = False

        if not ok:
            self._error_flags |= ERR_STOP
            self._log(self.DEBUG_DEFAULT, "Failed to stop communication")
            return False

        self._log(self.DEBUG_DEFAULT, "ECU disconnected")
        return True

    def request_sensors(self):
        """Request and decode the brand's sensor values.

        Returns True only if every request required by the brand succeeded.
        Values that could not be refreshed keep their previous value.
        """
        if not self._connected:
            self._error_flags |= ERR_USER
            return False

        if self._brand == BRAND_SUZUKI:
            if not self.handle_request(b"\x21\x08"):
                return False
            return self._decode_suzuki(self._response_payload)

        if self._brand == BRAND_KAWASAKI:
            ok = True
            for name, request in _KAWASAKI_REQUESTS:
                if not self.handle_request(request):
                    ok = False
                    continue
                value = self._decode_kawasaki(name, self._response_payload)
                if value is not None:
                    self._sensors[name] = value
            if ok:
                self._last_sensors = ticks_ms()
            return ok

        self._error_flags |= ERR_UNSUPPORTED
        return False

    def keep_alive(self, interval_ms=0):
        """Keep the connection alive. Call this regularly from the main loop.

        The default interval is 1500 ms. Returns False if the connection is
        closed or has expired (no valid response for 2 seconds).
        """
        self._drain_rx()
        if not self._connected:
            return False

        now = ticks_ms()
        age = ticks_diff(now, self._last_correct_response)
        if age >= ISO_T_P3_MAX:
            self._connected = False
            self._error_flags |= ERR_TIMEOUT
            self._log(self.DEBUG_DEFAULT, "Connection expired")
            return False

        if interval_ms == 0:
            interval_ms = KEEP_ALIVE_MS
        elif interval_ms > ISO_T_P3_MAX:
            interval_ms = KEEP_ALIVE_MS
            self._error_flags |= ERR_USER

        if age <= interval_ms:
            return True

        payload = b"\x21\x0B" if self._brand == BRAND_KAWASAKI else TESTER_PRESENT
        self._log(self.DEBUG_VERBOSE, "Keeping connection alive")
        self.handle_request(payload)
        return self._connected

    def handle_request(self, payload, attempts=3, check_response=True):
        """Send a raw request payload (header and checksum are added).

        Returns True when a positive response with a valid checksum arrives.
        ``attempts`` is clamped to 1..3. With ``check_response=False`` any
        complete checksum-valid frame counts as success.
        """
        self._ensure_uart()
        payload = bytes(payload)
        if len(payload) < 1 or len(payload) > 255:
            raise ValueError("payload length must be 1..255")

        attempts = int(attempts)
        if attempts < 1:
            attempts = 1
        elif attempts > 3:
            attempts = 3

        self._last_nrc = None
        code = -1
        got_frame = False

        for _ in range(attempts):
            frame = _build_request(
                payload, self._ecu_addr,
                self._use_length_byte, self._use_addresses,
            )
            self._send_request(frame)
            got_frame = self._listen_response()
            code = self._check_response(payload)
            self._last_response_code = code

            if check_response:
                if code == 0:
                    return True
            elif got_frame and self._response_checksum_ok:
                return True

        self._last_response_code = code
        return False

    def read_trouble_codes(self, which=READ_ONLY_ACTIVE):
        """Read diagnostic trouble codes.

        Returns a list of ``(code, status)`` tuples. ``status`` is None for
        ``READ_TOTAL``/``READ_ONLY_ACTIVE`` and an int for ``READ_ALL``. The
        raw response payload is available through ``last_response_payload``.
        """
        if not self._connected:
            self._error_flags |= ERR_USER
            return []

        services = {
            self.READ_TOTAL: b"\x13",
            self.READ_ONLY_ACTIVE: b"\x17",
            self.READ_ALL: b"\x18",
        }
        service = services.get(which)
        if service is None:
            raise ValueError("invalid trouble code selector: %r" % (which,))

        if not self.handle_request(service):
            return []

        payload = self._response_payload
        if len(payload) < 2:
            self._error_flags |= ERR_HEADER
            return []

        count = payload[1]
        width = 3 if which == self.READ_ALL else 2
        records = []
        pos = 2
        for _ in range(count):
            if pos + width > len(payload):
                self._error_flags |= ERR_HEADER
                break
            code = (payload[pos] << 8) | payload[pos + 1]
            if width == 3:
                records.append((code, payload[pos + 2]))
            else:
                records.append((code, None))
            pos += width
        return records

    def clear_trouble_codes(self, code=None):
        """Clear all DTCs (``code=None``) or one DTC (0..255)."""
        if not self._connected:
            self._error_flags |= ERR_USER
            return False

        if code is None:
            payload = b"\x14"
        else:
            code = int(code)
            if code < 0 or code > 0xFF:
                raise ValueError("code must be 0..255 or None")
            payload = bytes((0x14, code))
        return self.handle_request(payload)

    # -- status and errors ------------------------------------------------

    @property
    def connected(self):
        return self._connected

    @property
    def brand(self):
        return self._brand

    @property
    def brand_name(self):
        return _BRAND_NAMES[self._brand]

    @property
    def unit_system(self):
        return "metric" if self._metric else "imperial"

    @property
    def frame_options(self):
        return {
            "use_length_byte": self._use_length_byte,
            "use_addresses": self._use_addresses,
            "timing": self._timing,
        }

    @property
    def error_flags(self):
        return self._error_flags

    @property
    def has_error(self):
        return self._error_flags != 0

    @property
    def errors(self):
        names = []
        for bit, name in _ERROR_NAMES:
            if self._error_flags & bit:
                names.append(name)
        return tuple(names)

    def reset_error(self):
        """Clear all error flags."""
        self._error_flags = 0

    @property
    def last_response(self):
        """Last complete frame (header, payload, checksum) or b""."""
        return self._response

    @property
    def last_response_payload(self):
        """Payload of the last complete frame (no header or checksum)."""
        return self._response_payload

    @property
    def last_response_service(self):
        if not self._response_payload:
            return None
        return self._response_payload[0]

    @property
    def last_response_code(self):
        return self._last_response_code

    @property
    def last_nrc(self):
        return self._last_nrc

    # -- sensor properties ------------------------------------------------

    @property
    def gps(self):
        return self._sensors["gps"]

    @property
    def clutch(self):
        return self._sensors["clutch"]

    @property
    def rpm(self):
        return self._sensors["rpm"]

    @property
    def speed(self):
        value = self._sensors["speed"]
        if value is None or self._metric:
            return value
        return value / _KMH_TO_MPH

    @property
    def tps(self):
        return self._sensors["tps"]

    @property
    def stps(self):
        return self._sensors["stps"]

    @property
    def iap(self):
        return self._sensors["iap"]

    @property
    def iat(self):
        value = self._sensors["iat"]
        if value is None or self._metric:
            return value
        return value * 1.8 + 32.0

    @property
    def ect(self):
        value = self._sensors["ect"]
        if value is None or self._metric:
            return value
        return value * 1.8 + 32.0

    @property
    def voltage(self):
        return self._sensors["voltage"]

    @property
    def sensors(self):
        """All sensor values as a dict, with imperial conversion applied."""
        return {
            "gps": self.gps,
            "clutch": self.clutch,
            "rpm": self.rpm,
            "speed": self.speed,
            "tps": self.tps,
            "stps": self.stps,
            "iap": self.iap,
            "iat": self.iat,
            "ect": self.ect,
            "voltage": self.voltage,
        }

    # -- printing ---------------------------------------------------------

    def print_status(self, min_interval_ms=5000):
        """Print a status block. Throttled to once per *min_interval_ms*."""
        if not self._debug_enabled:
            return

        now = ticks_ms()
        if min_interval_ms and ticks_diff(now, self._last_status_print) < min_interval_ms:
            return
        self._last_status_print = now

        if self._last_correct_response:
            last = "%d ms ago" % ticks_diff(now, self._last_correct_response)
        else:
            last = "never"
        if self._connection_time:
            connected_for = "%d ms" % ticks_diff(now, self._connection_time)
        else:
            connected_for = "not connected"

        if self._dealer_enabled:
            dealer = "on" if self.dealer_mode else "off"
        else:
            dealer = "not enabled"

        lines = [
            "---- STATUS ----",
            "Connected: %s" % ("Yes" if self._connected else "No"),
            "Brand: %s" % _BRAND_NAMES[self._brand],
            "Frame: length byte=%s, addresses=%s, timing=%s" % (
                _tri_state(self._use_length_byte),
                _tri_state(self._use_addresses),
                "auto" if self._timing is None else self._timing,
            ),
            "Errors: %s" % (", ".join(self.errors) if self.errors else "none"),
            "Last correct response: %s" % last,
            "Connection time: %s" % connected_for,
            "Baudrate: %d" % ISO_BAUDRATE,
            "TX pin: %s" % str(self._tx_pin_arg),
            "Dealer mode: %s" % dealer,
        ]
        self._log(self.DEBUG_DEFAULT, "\n".join(lines))

    def print_sensors(self):
        """Print the last decoded sensor values."""
        if all(value is None for value in self._sensors.values()):
            self._error_flags |= ERR_USER
            if self._debug_enabled:
                self._log(self.DEBUG_DEFAULT, "No sensor data available")
            return
        if not self._debug_enabled:
            return

        speed_unit = "km/h" if self._metric else "mph"
        temp_unit = "C" if self._metric else "F"
        lines = [
            "---- SENSORS ----",
            "GPS: %s" % self.gps,
            "CLUTCH: %s" % self.clutch,
            "RPM: %s" % self.rpm,
            "Speed: %s %s" % (self.speed, speed_unit),
            "TPS: %s" % self.tps,
            "STPS: %s" % self.stps,
            "IAP: %s" % self.iap,
            "IAT: %s %s" % (self.iat, temp_unit),
            "ECT: %s %s" % (self.ect, temp_unit),
            "VOLT: %s" % self.voltage,
        ]
        self._log(self.DEBUG_DEFAULT, "\n".join(lines))

    def print_last_response(self):
        """Print the last complete response frame as hex."""
        if not self._debug_enabled:
            return
        if not self._response:
            self._log(self.DEBUG_DEFAULT, "No response")
        else:
            self._log(self.DEBUG_DEFAULT, _hex(self._response))

    # -- internal: UART and pin handling ----------------------------------

    def _ensure_uart(self):
        if self._uart is not None:
            return
        if self._uart_id is None:
            raise RuntimeError("no UART supplied and uart_id is not set")
        from machine import UART
        self._uart = UART(self._uart_id, **self._uart_init_kwargs())

    def _uart_init_kwargs(self):
        kwargs = dict(self._uart_kwargs)
        kwargs["baudrate"] = ISO_BAUDRATE
        if "pins" not in kwargs:
            if self._tx_pin_arg is not None:
                kwargs["tx"] = self._tx_pin_arg
            if self._rx_pin_arg is not None:
                kwargs["rx"] = self._rx_pin_arg
        return kwargs

    def _release_uart(self):
        if self._uart_id is None or self._uart is None:
            return
        try:
            self._uart.deinit()
        except (AttributeError, OSError):
            pass

    def _reinit_uart(self):
        kwargs = self._uart_init_kwargs()
        if self._uart_id is not None:
            # Mirror the C++ Serial.end()/begin(): create a fresh UART after
            # the TX pin was driven as a GPIO for the fast-init waveform.
            try:
                from machine import UART
                self._uart = UART(self._uart_id, **kwargs)
                return
            except Exception as exc:
                raise RuntimeError("could not re-initialize UART: %s" % exc)

        self._ensure_uart()
        try:
            self._uart.init(**kwargs)
        except (AttributeError, TypeError, OSError, ValueError):
            raise RuntimeError(
                "UART.init() failed to reattach the K-line pins; "
                "pass uart_id or create a new UART object"
            )

    def _get_tx_pin(self):
        if self._tx_pin_obj is None:
            if isinstance(self._tx_pin_arg, int):
                try:
                    from machine import Pin
                except ImportError:
                    raise RuntimeError("machine.Pin is required to drive tx_pin")
                self._tx_pin_obj = Pin(self._tx_pin_arg, Pin.OUT, value=1)
            else:
                self._tx_pin_obj = self._tx_pin_arg
        return self._tx_pin_obj

    def _drain_rx(self):
        if self._uart is None:
            return
        while self._uart.any():
            if self._uart.read(1) is None:
                break

    # -- internal: protocol ----------------------------------------------

    def _send_request(self, frame):
        self._log(self.DEBUG_VERBOSE, "Sending: %s" % _hex(frame))

        for byte in frame:
            self._uart.write(bytes((byte,)))
            sleep_ms(ISO_T_P4_MIN)

            # Read at most one echo byte per transmitted byte. Do not drain
            # everything: the ECU response may already sit behind the echo.
            if self._uart.any():
                echo = self._uart.read(1)
                if echo:
                    if echo[0] != byte:
                        self._error_flags |= ERR_ECHO
                    self._log(self.DEBUG_VERBOSE, "Echo: %02X" % echo[0])

        try:
            self._uart.flush()
        except AttributeError:
            pass
        sleep_ms(ISO_T_P2_MIN)

    def _listen_response(self):
        self._response = b""
        self._response_payload = b""
        self._response_header_len = 0
        self._response_checksum_ok = False

        buf = bytearray()
        header = None
        last_rx = ticks_ms()

        while True:
            if header is None:
                header = _response_geometry(buf, self._use_addresses)
                if header is not None and len(buf) >= header[2]:
                    break
            elif len(buf) >= header[2]:
                break

            if ticks_diff(ticks_ms(), last_rx) >= ISO_T_P3_MAX:
                break

            if self._uart.any():
                chunk = self._uart.read(1)
                if chunk:
                    buf.append(chunk[0])
                    last_rx = ticks_ms()
            else:
                sleep_ms(1)

        sleep_ms(ISO_T_P3_MIN)

        if header is None or len(buf) < header[2]:
            return False

        header_len, _data_len, total, addresses = header
        frame = bytes(buf[:total])
        payload, checksum_ok, errors = _parse_response(
            frame, header_len, addresses, self._ecu_addr
        )

        self._response = frame
        self._response_header_len = header_len
        self._response_payload = payload
        self._response_checksum_ok = checksum_ok
        self._error_flags |= errors

        if checksum_ok:
            self._last_correct_response = ticks_ms()
            if self._use_length_byte is None:
                self._use_length_byte = ((frame[0] & 0x3F) == 0)
            if self._use_addresses is None:
                self._use_addresses = addresses

        self._log(self.DEBUG_VERBOSE, "Receiving: %s" % _hex(frame))
        return True

    def _check_response(self, request_payload):
        if not self._response or not self._response_checksum_ok:
            return -1

        payload = self._response_payload
        if not payload:
            return -1

        if payload[0] == (request_payload[0] | 0x40):
            return 0

        if payload[0] == REJECTED:
            if len(payload) < 3:
                self._error_flags |= ERR_RESPONSE | ERR_HEADER
                return -8
            if payload[1] != request_payload[0]:
                self._error_flags |= ERR_WRONG_REJECT
            self._error_flags |= ERR_RESPONSE
            nrc = payload[2]
            self._last_nrc = nrc
            return {
                0x10: -2,
                0x11: -3,
                0x12: -4,
                0x21: -5,
                0x22: -6,
                0x78: -7,
            }.get(nrc, -8)

        self._error_flags |= ERR_RESPONSE | ERR_UNEXPECTED
        return -9

    # -- internal: decoding ----------------------------------------------

    def _decode_suzuki(self, payload):
        # Highest accessed payload index is 48 (clutch).
        if len(payload) < 49:
            self._error_flags |= ERR_HEADER
            return False

        base = 2
        rpm_h = payload[base + _SUZUKI_OFFSETS["rpm_h"]]
        rpm_l = payload[base + _SUZUKI_OFFSETS["rpm_l"]]

        values = {
            "gps": payload[base + _SUZUKI_OFFSETS["gps"]],
            "clutch": payload[base + _SUZUKI_OFFSETS["clutch"]],
            "rpm": rpm_h * 10 + rpm_l / 10,
            "speed": payload[base + _SUZUKI_OFFSETS["speed"]] * 2,
            "tps": 125 * (payload[base + _SUZUKI_OFFSETS["tps"]] - 55) / (256 - 55),
            "stps": payload[base + _SUZUKI_OFFSETS["stps"]] / 2.55,
            "iap": payload[base + _SUZUKI_OFFSETS["iap"]] * 4 * 0.136,
            "iat": (payload[base + _SUZUKI_OFFSETS["iat"]] - 48) / 1.6,
            "ect": (payload[base + _SUZUKI_OFFSETS["ect"]] - 48) / 1.6,
            "voltage": payload[base + _SUZUKI_OFFSETS["voltage"]] * 100 / 126,
        }
        self._sensors.update(values)
        self._last_sensors = ticks_ms()
        return True

    def _decode_kawasaki(self, name, payload):
        if len(payload) < 3:
            self._error_flags |= ERR_HEADER
            return None

        raw = payload[2]
        if name == "rpm":
            if len(payload) < 4:
                self._error_flags |= ERR_HEADER
                return None
            return ((payload[2] * 255 + payload[3]) / 255.0) * 100
        if name in ("gps", "speed", "tps"):
            return raw
        if name == "iap":
            return raw * 4 * 0.136
        if name == "iat" or name == "ect":
            return (raw - 48) / 1.6
        return None

    def _log(self, level, text):
        if not self._debug_enabled or self._debug_level < level:
            return
        stream = self._debug if self._debug is not None else sys.stdout
        try:
            stream.write(str(text) + "\n")
        except Exception:
            self._debug_enabled = False


# Expose the error bits on the class too, so users can write KWP2000.ERR_START.
KWP2000.ERR_TEST = ERR_TEST
KWP2000.ERR_START = ERR_START
KWP2000.ERR_STOP = ERR_STOP
KWP2000.ERR_TO = ERR_TO
KWP2000.ERR_FROM = ERR_FROM
KWP2000.ERR_CHECKSUM = ERR_CHECKSUM
KWP2000.ERR_ECHO = ERR_ECHO
KWP2000.ERR_UNEXPECTED = ERR_UNEXPECTED
KWP2000.ERR_HEADER = ERR_HEADER
KWP2000.ERR_USER = ERR_USER
KWP2000.ERR_CONFIG = ERR_CONFIG
KWP2000.ERR_TIMEOUT = ERR_TIMEOUT
KWP2000.ERR_RESPONSE = ERR_RESPONSE
KWP2000.ERR_TIMING = ERR_TIMING
KWP2000.ERR_WRONG_REJECT = ERR_WRONG_REJECT
KWP2000.ERR_UNSUPPORTED = ERR_UNSUPPORTED
