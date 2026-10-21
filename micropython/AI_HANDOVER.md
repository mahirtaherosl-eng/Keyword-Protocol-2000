# AI Handover Notes: KWP2000 MicroPython Library

**Module version:** `0.1.0`
**Last verified:** 2026-10-20
**Audience:** an AI coding agent writing MicroPython programs for a microcontroller.
**Source of truth:** [`kwp2000.py`](kwp2000.py). If this document disagrees with the code, the code wins.

This document is self-contained. Read the ten rules below, then use the API reference and the
template closest to the requested program. Do not invent methods or copy names from the Arduino
C++ library in the parent repository.

**Legend:** `[V]` = verified in the source/tests, `[P]` = recommended pattern.

**Contents**

1. [Read this first: ten rules](#1-read-this-first-ten-rules)
2. [Hardware and board setup](#2-hardware-and-board-setup)
3. [Install and import](#3-install-and-import)
4. [Exact API reference](#4-exact-api-reference)
5. [Programming model, timing and failure semantics](#5-programming-model-timing-and-failure-semantics)
6. [Program templates](#6-program-templates)
7. [Prioritized pitfalls and decision rules](#7-prioritized-pitfalls-and-decision-rules)
8. [Validation checklist for generated programs](#8-validation-checklist-for-generated-programs)
9. [Testing and verification](#9-testing-and-verification)
10. [Known limitations](#10-known-limitations)
11. [Appendices](#11-appendices)

---

## 1. Read this first: ten rules

1. **The K-line is 12 V.** Never connect an MCU pin directly. Require an L9637, MC33660,
   MC33199, or equivalent interface, and a common ground with the bike.
2. **Import only what exists:** `from kwp2000 import KWP2000`. Do not use Arduino names such as
   `getRPM()`, `initKline()`, `requestSensorsData()`, or `clearTroubleCodes(0)`.
3. **`tx_pin` is mandatory.** It is both the UART TX pin and the GPIO driven during the fast-init
   waveform. Pass it to both `UART(...)` and `KWP2000(...)`.
4. **Call `connect()` and check the return value** before any high-level request. On failure,
   print `ecu.errors` and retry with a delay.
5. **Call `keep_alive()` at least every 2 seconds while connected.** The library never
   auto-reconnects and never runs anything in the background.
6. **Every sensor property starts as `None`.** A failed refresh keeps the previous value. Guard
   every value before formatting or arithmetic.
7. **Clear-all is `clear_trouble_codes()` (or `None`).** `clear_trouble_codes(0)` clears DTC 0.
8. **The API is blocking.** Budget for multi-second worst cases and feed or disable the watchdog.
9. **Yamaha and Honda are raw-request-only stubs.** `request_sensors()` returns `False` and sets
   `ERR_UNSUPPORTED`; use `handle_request()` and label the result raw.
10. **Use `disconnect()` in a `finally` block** and implement explicit reconnect/backoff. Do not
    call the library from an interrupt or a second thread.

### If you only read one paragraph

```python
from machine import UART
from kwp2000 import KWP2000

uart = UART(2, baudrate=10400, tx=17, rx=16)
ecu = KWP2000(uart, "suzuki", tx_pin=17, rx_pin=16)

if ecu.connect():
    if ecu.request_sensors():
        print(ecu.rpm, ecu.speed, ecu.ect)   # values may be None
    ecu.keep_alive()                        # call repeatedly while connected
    ecu.disconnect()
else:
    print("connect failed:", ecu.errors)
```

---

## 2. Hardware and board setup

### 2.1 Electrical requirements `[V]`

- The K-line is a **12 V single-wire bus**. Use an interface IC:
  **L9637**, **MC33660**, **MC33199**, or an equivalent level shifter/transistor circuit.
- Connect the MCU ground, interface ground, and bike ground together.
- Wiring references:
  - [L9637 wiring](../extras/Images/l9637_wiring.png)
  - [Suzuki dealer-mode optocoupler](../extras/Images/dealer_mode.png)
- The library reads one echoed byte per transmitted byte. A missing echo is tolerated; a
  **mismatched** echo sets `ERR_ECHO` (advisory).

### 2.2 UART and pins `[V]`

- The K-line UART runs at **10400 baud**. The library forces 10400 on every `connect()`.
- Bits/parity/stop are not configured by the library; MicroPython's default **8N1** is assumed.
- `tx_pin` (int or `machine.Pin`) is required. It is the UART TX and is temporarily driven as a
  GPIO to generate the fast-init waveform.
- `rx_pin` (int or `machine.Pin`) is used when the UART is re-initialized. It is optional but
  strongly recommended.
- When `uart_id` is supplied, `connect()` deinitializes the UART before the fast-init waveform
  and creates a fresh `machine.UART` afterward, mirroring the C++ `end()`/`begin()` lifecycle.
  Without `uart_id`, it reinitializes the existing UART with `tx`/`rx` after the waveform.
- If `uart_kwargs` contains `"pins"`, the library does **not** add `tx`/`rx`. Use that for ports
  whose `UART.init()` takes a `pins=` tuple instead.

### 2.3 Board examples `[P]`

**ESP32 classic / WROOM** (used by `examples/basic_working.py`):

```python
from machine import UART
from kwp2000 import KWP2000

UART_ID = 2
TX_PIN = 17
RX_PIN = 16

uart = UART(UART_ID, baudrate=10400, tx=TX_PIN, rx=RX_PIN)
ecu = KWP2000(uart, "suzuki", tx_pin=TX_PIN, rx_pin=RX_PIN)
```

**ESP32-C6** (used by `examples/kawasaki_wifi_dashboard.py`; different UART map):

```python
from machine import UART
from kwp2000 import KWP2000

UART_ID = 1
TX_PIN = 19
RX_PIN = 20

uart = UART(UART_ID, baudrate=10400, tx=TX_PIN, rx=RX_PIN)
ecu = KWP2000(uart, "kawasaki", tx_pin=TX_PIN, rx_pin=RX_PIN)
```

The dashboard also switches the L9637 logic VCC from GPIO22 (high while running, low on exit).
On the generic C6 firmware UART0 (GPIO16/17) is reserved for the REPL; GPIO10/11 are unusable on
this board (a loopback returned 0x00). Use UART1 pins that pass a plain loopback; 19/20 are the
dashboard defaults, and 1/2 or 18/19 are alternatives. Avoid GPIO4/5/8/9/15 (strapping), 12/13
(USB), 16/17 (UART0), and 24-30 (SPI flash).

**Raspberry Pi Pico / RP2040** (adjust to your board's free UART and pins):

```python
from machine import UART
from kwp2000 import KWP2000

UART_ID = 1
TX_PIN = 4
RX_PIN = 5

uart = UART(UART_ID, baudrate=10400, tx=TX_PIN, rx=RX_PIN)
ecu = KWP2000(uart, "suzuki", tx_pin=TX_PIN, rx_pin=RX_PIN)
```

**STM32 / other ports:** use the board's UART id and pin names, for example
`UART(1, baudrate=10400, tx=Pin("PA2"), rx=Pin("PA3"))`. Keep the same `tx_pin`/`rx_pin` in the
`KWP2000(...)` call.

**Library-managed UART** (alternative):

```python
ecu = KWP2000(None, "suzuki", tx_pin=17, rx_pin=16, uart_id=2)
```

The UART is then created on `connect()` at 10400 baud.

### 2.4 Fast-init waveform `[V]`

On each `connect()` the library does:

| Step | Action | Duration |
| --- | --- | --- |
| 1 | TX pin configured as output, level HIGH | 1000 ms |
| 2 | TX LOW | 25 ms |
| 3 | TX HIGH | 25 ms |
| 4 | UART reinitialized/recreated at 10400 and handshake sent | — |

Do not replace this with a direct UART write. The waveform is required to wake the ECU.

### 2.5 Suzuki dealer mode `[P]`

- Suzuki only.
- Requires an optocoupler and a ~330 ohm resistor between the MCU and the bike's dealer pin.
- Pass the control pin to `enable_dealer_mode(pin)`; the pin is initialized LOW (off).
- `dealer_mode` is `None` until dealer mode has been enabled.

---

## 3. Install and import

### 3.1 Install `[V]`

From the repository root manifest (`package.json`):

```sh
mpremote mip install github:mahirtaherosl-eng/Keyword-Protocol-2000
```

This installs `kwp2000.py` into the board's `lib` directory.

Manual copy:

```sh
mpremote fs cp micropython/kwp2000.py :lib/kwp2000.py
```

Older `mip` versions that do not resolve the relative manifest URL can install the file directly:

```sh
mpremote mip install github:mahirtaherosl-eng/Keyword-Protocol-2000/micropython/kwp2000.py
```

> **Upstream note:** the upstream `aster94/Keyword-Protocol-2000` repository does not contain the
> `micropython/` directory or root `package.json`; the port is published from
> `mahirtaherosl-eng/Keyword-Protocol-2000`. If you publish your own fork, substitute its URL. You
> can always copy `kwp2000.py` to the board manually.

### 3.2 Import `[V]`

```python
from kwp2000 import KWP2000
```

The module has no dependencies beyond MicroPython's `time`, `sys`, and (lazily) `machine`.

---

## 4. Exact API reference

### 4.1 Constructor

```text
KWP2000(uart, brand, tx_pin=None, rx_pin=None, *,
        uart_id=None, uart_kwargs=None, debug=None,
        debug_level=DEBUG_NONE)
```

| Parameter | Meaning |
| --- | --- |
| `uart` | A `machine.UART`-like object. May be `None` only when `uart_id` is supplied. Must provide `write`, `read`, `any`, and `init`; `flush` is optional. |
| `brand` | `KWP2000.SUZUKI`, `KAWASAKI`, `YAMAHA`, `HONDA`, or a case-insensitive string (`"suzuki"`). |
| `tx_pin` | UART TX pin and fast-init GPIO. Int or `machine.Pin`. Required by `connect()`. |
| `rx_pin` | UART RX pin used when reinitializing. Int or `machine.Pin`. Recommended. |
| `uart_id` | UART peripheral id. Required when `uart=None`. When supplied, the UART is deinitialized before the fast-init waveform and recreated afterward. |
| `uart_kwargs` | Extra `uart.init()` kwargs, e.g. `{"timeout": 10, "rxbuf": 256}`. `baudrate` is forced to 10400. If `"pins"` is present, `tx`/`rx` are not added. |
| `debug` | Stream with `write()`. Defaults to `sys.stdout` when debug is enabled. |
| `debug_level` | `DEBUG_NONE` (default), `DEBUG_DEFAULT`, or `DEBUG_VERBOSE`. Debug is silent unless this is > `DEBUG_NONE`. |

### 4.2 Class constants `[V]`

```python
# Brands
KWP2000.SUZUKI = 0
KWP2000.KAWASAKI = 1
KWP2000.YAMAHA = 2
KWP2000.HONDA = 3

# Debug levels
KWP2000.DEBUG_NONE = 0
KWP2000.DEBUG_DEFAULT = 1
KWP2000.DEBUG_VERBOSE = 2

# Trouble-code selectors
KWP2000.READ_TOTAL = 0          # service 0x13
KWP2000.READ_ONLY_ACTIVE = 1    # service 0x17 (default)
KWP2000.READ_ALL = 2            # service 0x18
```

Error bits (also module-level `kwp2000.ERR_*`):

| Bit | Hex | Name | Meaning |
| --- | --- | --- | --- |
| 0 | 0x0001 | `ERR_TEST` | Reserved; never set by this code. |
| 1 | 0x0002 | `ERR_START` | Start communication / Kawasaki diagnostic handshake failed. |
| 2 | 0x0004 | `ERR_STOP` | Stop communication failed. |
| 3 | 0x0008 | `ERR_TO` | Response target address was not our address. |
| 4 | 0x0010 | `ERR_FROM` | Response source address was not the ECU. |
| 5 | 0x0020 | `ERR_CHECKSUM` | Response checksum mismatch. |
| 6 | 0x0040 | `ERR_ECHO` | Echo byte mismatch (advisory). |
| 7 | 0x0080 | `ERR_UNEXPECTED` | Response service was not the expected positive service. |
| 8 | 0x0100 | `ERR_HEADER` | Malformed/unsupported header, or short payload. |
| 9 | 0x0200 | `ERR_USER` | Invalid state/argument for the operation (e.g. not connected). |
| 10 | 0x0400 | `ERR_CONFIG` | Key-byte configuration error. |
| 11 | 0x0800 | `ERR_TIMEOUT` | 2 s keep-alive expiry. |
| 12 | 0x1000 | `ERR_RESPONSE` | Negative response, or an unexpected response service (with `ERR_UNEXPECTED`). |
| 13 | 0x2000 | `ERR_TIMING` | Reserved; never set. |
| 14 | 0x4000 | `ERR_WRONG_REJECT` | Negative response rejected a different service. |
| 15 | 0x8000 | `ERR_UNSUPPORTED` | Unsupported brand/timing mode. |

### 4.3 Methods

| Method | Returns | Semantics |
| --- | --- | --- |
| `connect()` | `bool` | Runs the fast-init waveform and handshake. Blocks ~1.1-1.3 s on success; ~3.2 s if the start handshake fails; Kawasaki can take ~7.5 s if the start succeeds but the diagnostic session fails. Returns `True` if already connected. Resets errors only when it actually starts a new connection. |
| `disconnect()` | `bool` | Sends stop communication, waits 2000 ms, marks disconnected. ~2.1 s on success; ~8.5 s if all stop attempts time out. Preserves error flags and sensor values. Returns `True` if already disconnected. |
| `request_sensors()` | `bool` | Suzuki: one request and a successful decode. Kawasaki: seven sequential requests; returns `True` only if every `handle_request()` succeeded (a short Kawasaki payload can still set `ERR_HEADER` while returning `True`). Sensor values refresh individually; failed values keep the old value. Requires connection. |
| `keep_alive(interval_ms=0)` | `bool` | Drains RX, enforces the 2 s expiry, and sends tester-present (`3E 01`) or Kawasaki GPS (`21 0B`) when the link has been quiet longer than `interval_ms`. `0` = 1500 ms; values >2000 are clamped to 1500 and set `ERR_USER`. Returns `self.connected`, **not** whether its request succeeded. |
| `handle_request(payload, attempts=3, check_response=True)` | `bool` | Raw request. `payload` is service byte + parameters only (1..255 bytes); the library adds header/length/addresses/checksum. `attempts` is clamped to 1..3. Does **not** check or set `connected`. With `check_response=False`, any complete checksum-valid frame returns `True`, but error flags/`last_nrc` are still updated. |
| `read_trouble_codes(which=READ_ONLY_ACTIVE)` | `list[tuple[int, int|None]]` | Returns `(code, status)` tuples. 2-byte records for `0x13`/`0x17`; 3-byte records for `0x18`. `[]` is ambiguous: no DTCs or a failed request. Partial data sets `ERR_HEADER`. Invalid selector raises `ValueError` only when connected. |
| `clear_trouble_codes(code=None)` | `bool` | `None` sends `0x14` (clear all). An int 0..255 sends `0x14 <code>` (one byte). `0` is a valid single code, not clear-all. Invalid code raises `ValueError` only when connected. |
| `enable_debug(stream=None, level=DEBUG_DEFAULT)` | `None` | Enables debug output. `stream=None` uses `sys.stdout`. |
| `set_debug_level(level)` | `None` | Sets the level; level > `DEBUG_NONE` enables output. |
| `disable_debug()` | `None` | Disables output; stream/level remain stored. |
| `print_status(min_interval_ms=5000)` | `None` | Prints connection/frame/error status if debug is enabled. Throttled to once per interval; pass `0` to force. |
| `print_sensors()` | `None` | Prints decoded sensor values if debug is enabled. Sets `ERR_USER` when all values are `None`. |
| `print_last_response()` | `None` | Hex-dumps the last complete frame if debug is enabled. |
| `use_imperial()` / `use_metric()` | `None` | Changes only the returned speed/temperature units. Stored values are not modified. |
| `reset_error()` | `None` | Clears the error bitmask. |
| `enable_dealer_mode(pin)` | `bool` | Suzuki only. Configures an output pin (int or `machine.Pin`) and returns `True`; other brands return `False` + `ERR_USER`. |
| `set_dealer_mode(enabled)` | `bool` | Drives the dealer pin. Returns `False` + `ERR_USER` if dealer mode was not enabled. |
| `dealer_mode` (property) | `bool` or `None` | Current dealer pin state; `None` until enabled. |

### 4.4 Properties

| Property | Type | Meaning |
| --- | --- | --- |
| `connected` | `bool` | Session flag. Becomes stale if `keep_alive()` is not called. |
| `brand` / `brand_name` | `int` / `str` | Normalized brand. |
| `unit_system` | `str` | `"metric"` or `"imperial"`. |
| `frame_options` | `dict` | `{"use_length_byte", "use_addresses", "timing"}`. The first two are `True`, `False`, or `None` (auto/unresolved); `timing` is `"normal"`, `"extended"`, or `None`. Timing is informational. |
| `error_flags` | `int` | Accumulated error bitmask. |
| `has_error` | `bool` | `error_flags != 0`. A successful `connect()` can still set flags. |
| `errors` | `tuple[str, ...]` | Names of set bits. |
| `last_response` | `bytes` | Last complete frame including header and checksum; `b""` if none. |
| `last_response_payload` | `bytes` | Last payload without header/checksum. |
| `last_response_service` | `int` or `None` | Payload byte 0 (e.g. `0x7F` for a negative response). |
| `last_response_code` | `int` or `None` | `None` before the first request; `0` positive; `-1` no response/bad checksum; `-2..-8` NRC mapping; `-9` unexpected service. |
| `last_nrc` | `int` or `None` | Raw negative response code. Reset once per `handle_request()`, so it may be from an earlier attempt. |
| `sensors` | `dict` | All sensor values with unit conversion applied. |
| `gps`, `clutch`, `rpm`, `speed`, `tps`, `stps`, `iap`, `iat`, `ect`, `voltage` | number or `None` | Sensor values; see the table below. |

### 4.5 Sensor values by brand `[V]`

All sensor properties start as `None`. Values persist across failed refreshes, `disconnect()`, and
`reconnect()`; they are not reset. Computed values use Python true division and may be floats.

| Property | Suzuki | Kawasaki |
| --- | --- | --- |
| `gps` | raw byte (gear) | raw byte |
| `clutch` | raw byte | always `None` |
| `rpm` | `rpm_h * 10 + rpm_l / 10` (float) | `((b2 * 255 + b3) / 255.0) * 100` (float) |
| `speed` | `raw * 2` km/h | one byte: raw (not km/h); two bytes: `(b2 * 100 + b3) / 2` km/h |
| `tps` | `125 * (raw - 55) / 201` (can be negative) | one byte: raw (not percent); two bytes: `125 * (u16 - 201) / (405 - 201)` with `u16 = b2 * 256 + b3` |
| `stps` | `raw / 2.55` | always `None` |
| `iap` | `raw * 4 * 0.136` | same formula |
| `iat` | `(raw - 48) / 1.6` °C | same formula |
| `ect` | `(raw - 48) / 1.6` °C | same formula |
| `voltage` | `raw * 100 / 126` V | always `None` |

Kawasaki speed and TPS use the two-byte formulas when the ECU reply carries more than one data
byte (`len(payload) >= 4`); a one-byte reply keeps the raw value. The two-byte TPS value is
big-endian and the documented range is 201..405 (for example `00 DE` = 222). Two-byte speed is a
decimal pair: the first byte is hundreds, the second is the remainder.

Unit conversion is read-only:

- `use_imperial()`: `speed / 1.609344` (mph), `iat`/`ect * 1.8 + 32` (°F).
- `use_metric()`: restores km/h and °C.
- `sensors` applies the same conversion.
- No other values are converted.

### 4.6 Return and exception conventions `[V]`

- Protocol failures return `False`, `[]`, or `None`, and set error flags. They do **not** raise.
- Invalid arguments raise `ValueError` — but for `read_trouble_codes()` and
  `clear_trouble_codes()` the connection check happens first, so while disconnected they return
  `[]`/`False` + `ERR_USER` instead of raising.
- Construction without `uart` or `uart_id` raises `ValueError`. Missing hardware raises
  `RuntimeError` (no `tx_pin`, or the UART cannot be reinitialized). Unsupported brand features
  return `False` with `ERR_UNSUPPORTED` instead of raising.
- The port does **not** use Arduino-style negative return numbers.

---

## 5. Programming model, timing and failure semantics

### 5.1 Lifecycle `[V]`

1. Construct `KWP2000` with a UART (or `uart_id`), brand, `tx_pin`, and `rx_pin`.
2. Call `connect()` once and check the result.
3. While connected, call `request_sensors()`, `read_trouble_codes()`, `clear_trouble_codes()`,
   `handle_request()`, and `keep_alive()`.
4. Call `disconnect()` before shutting down, ideally in a `finally` block.
5. On failure/expiry, call `connect()` again. There is no auto-reconnect.

Everything is synchronous and blocking. There is no async, thread, callback, interrupt, or
context-manager support.

### 5.2 What refreshes the connection `[V]`

`_last_correct_response` is refreshed whenever a **complete frame with a valid checksum** arrives.
That includes positive responses, negative/NRC responses, unexpected service responses, and
responses with wrong target/source addresses. "2 seconds without a response" means 2 seconds
without a checksum-valid frame, not without a successful request.

Expiry is enforced **only inside `keep_alive()`**. If `keep_alive()` is not called, `connected`
can remain `True` after the ECU has stopped responding. `handle_request()` neither checks nor sets
`connected`.

### 5.3 Keep-alive `[V]`

```python
ecu.keep_alive()            # default interval 1500 ms
ecu.keep_alive(1000)        # custom interval
```

- `interval_ms == 0` selects the default 1500 ms; it does **not** mean "send now".
- Values >2000 are clamped to 1500 and set `ERR_USER`.
- If `age >= 2000` at entry: sets `connected = False`, sets `ERR_TIMEOUT`, returns `False`.
- If `age <= interval_ms`: returns `True` without sending.
- Otherwise sends tester-present (`3E 01`) or, for Kawasaki, a GPS request (`21 0B`), then returns
  `self.connected`. A `True` return does **not** guarantee that the keep-alive request succeeded.
- Kawasaki keep-alive does **not** decode or update `gps`.

Call it every loop iteration, or at least every 1 second. If sensor/DTC polling is more frequent
than ~1.5 s, those requests also refresh the link, but still call `keep_alive()` as the documented
pattern.

### 5.4 Blocking and retry budget `[V]`

Per attempt: each transmitted byte is written separately with a 10 ms gap, then 25 ms P2, then up
to 2000 ms for a response, then 55 ms P3. A silent-bus attempt is ~2.1-2.2 s.

| Operation | Worst-case blocking on a silent bus |
| --- | --- |
| `connect()` success | ~1.1-1.3 s |
| `connect()` failure | ~3.2 s if the start handshake fails; Kawasaki ~7.5 s if the diagnostic session then fails |
| `disconnect()` success | ~2.1 s |
| `disconnect()` failure | ~8.5 s |
| `handle_request()` default (3 attempts) | ~6.3-6.5 s |
| Suzuki `request_sensors()` failure | ~6.5 s |
| Kawasaki `request_sensors()` failure | ~44-45 s (7 requests × 3 attempts) |
| `keep_alive()` when it decides to transmit, silent bus | ~6.5 s |

Retry counts: start communication 1; Kawasaki diagnostic session 3; stop communication 3; all
other requests 3. `handle_request()` clamps `attempts` to 1..3. There is no backoff.

**Watchdog guidance `[P]`:**
- Budget for the worst case above; feed the watchdog before/after long calls or disable it during
  hardware bring-up.
- Use `handle_request(..., attempts=1)` for raw/diagnostic requests where a single attempt is
  acceptable.
- High-level methods do not expose an attempts parameter.
- Do not treat the 2000 ms response timeout as a total deadline: it is an inter-byte timeout and a
  slow stream can extend a response.

### 5.5 Error lifecycle `[V]`

- Error flags accumulate. Clear them with `reset_error()` or by starting a new connection.
- `connect()` on an already-connected object returns `True` **without** resetting errors.
- A successful `connect()` can still return `True` with `ERR_CONFIG` and/or `ERR_UNSUPPORTED` set
  from key-byte negotiation.
- `ERR_TO`/`ERR_FROM` can accompany a checksum-valid positive response; decide whether to treat
  them as fatal.
- `ERR_ECHO` is advisory.
- `ERR_TEST` and `ERR_TIMING` are defined but never set.
- `disconnect()` preserves error flags.

### 5.6 Response diagnostics `[V]`

- `last_response_code`: `None` before the first request, then `0` positive, `-1` no response or
  bad checksum, `-2..-8` negative response codes, `-9` unexpected service.
- `last_nrc`: raw NRC byte; reset once per `handle_request()`, not per attempt.
- Frame fields (`last_response`, `last_response_payload`, `last_response_service`) are cleared at
  the start of every attempt and by `disconnect()`; after retries, only the final attempt's frame
  remains. `last_response_code` and `last_nrc` are not cleared by `disconnect()`.
- `last_response` includes header + checksum; `last_response_payload` excludes both.

---

## 6. Program templates

All templates are `[P]` recommendations built on the verified API above. Templates A and B are
standalone programs. Templates C-F are snippets to add after constructing and connecting `ecu`
with Template A or B (they assume `ecu` is in scope; Template D additionally checks
`ecu.connected`).

### 6.0 Shared configuration block

```python
# --- EDIT FOR YOUR BOARD ---
BRAND = "suzuki"       # "suzuki" or "kawasaki"
UART_ID = 2            # ESP32 UART2; Pico usually 1
TX_PIN = 17            # UART TX / fast-init GPIO
RX_PIN = 16            # UART RX
DEALER_PIN = None      # e.g. 4 for Suzuki dealer mode
```

### 6.1 Template A — minimal connect, read, disconnect

```python
import time
from machine import UART
from kwp2000 import KWP2000

BRAND = "suzuki"
UART_ID = 2
TX_PIN = 17
RX_PIN = 16

uart = UART(UART_ID, baudrate=10400, tx=TX_PIN, rx=RX_PIN)
ecu = KWP2000(uart, BRAND, tx_pin=TX_PIN, rx_pin=RX_PIN)
ecu.enable_debug(level=KWP2000.DEBUG_DEFAULT)

try:
    if not ecu.connect():
        print("connect failed:", ecu.errors)
    elif ecu.request_sensors():
        print("RPM:", "n/a" if ecu.rpm is None else ecu.rpm)
        print("Speed:", "n/a" if ecu.speed is None else ecu.speed)
        print("ECT:", "n/a" if ecu.ect is None else ecu.ect)
    else:
        print("sensor request failed:", ecu.errors)
finally:
    ecu.disconnect()
```

### 6.2 Template B — persistent polling with keep-alive and reconnect

```python
import time
from machine import UART
from kwp2000 import KWP2000

BRAND = "suzuki"
UART_ID = 2
TX_PIN = 17
RX_PIN = 16

uart = UART(UART_ID, baudrate=10400, tx=TX_PIN, rx=RX_PIN)
ecu = KWP2000(uart, BRAND, tx_pin=TX_PIN, rx_pin=RX_PIN)


def show_sensors():
    if not ecu.request_sensors():
        print("sensor read failed:", ecu.errors)
        return
    print("RPM:", "n/a" if ecu.rpm is None else ecu.rpm)
    print("Speed:", "n/a" if ecu.speed is None else ecu.speed)
    print("ECT:", "n/a" if ecu.ect is None else ecu.ect)


try:
    while True:
        if not ecu.connected:
            if not ecu.connect():
                print("connect failed:", ecu.errors)
                time.sleep(2)
                continue

        show_sensors()

        # ~1 s of keep-alive; stay well below the 2 s expiry.
        for _ in range(100):
            if not ecu.keep_alive():
                print("link lost:", ecu.errors)
                break
            time.sleep_ms(10)
finally:
    ecu.disconnect()
```

### 6.3 Template C — read and clear trouble codes

```python
from kwp2000 import KWP2000


def show_dtcs(which=KWP2000.READ_ALL):
    if not ecu.connected:
        print("not connected")
        return
    before = ecu.error_flags
    codes = ecu.read_trouble_codes(which)
    new_errors = ecu.error_flags & ~before
    if ecu.last_response_code != 0 or (new_errors & KWP2000.ERR_HEADER):
        print("DTC read failed:", ecu.errors)
        return
    if not codes:
        print("No DTCs")
        return
    for code, status in codes:
        if status is None:
            print("DTC 0x%04X" % code)
        else:
            print("DTC 0x%04X status 0x%02X" % (code, status))


def clear_all_dtcs():
    if ecu.clear_trouble_codes():          # None -> clear all
        print("DTCs cleared")
    else:
        print("clear failed:", ecu.errors)


def clear_one_dtc_byte(code):
    # The library's clear parameter is ONE byte, 0..255.
    if code < 0 or code > 0xFF:
        print("clear_trouble_codes() takes one byte; use handle_request() for 16-bit codes")
        return
    if ecu.clear_trouble_codes(code):
        print("clear request accepted for 0x%02X" % code)
    else:
        print("clear failed:", ecu.errors)


def clear_two_byte_dtc(code):
    # Only if your ECU's service 0x14 expects a two-byte DTC.
    payload = bytes((0x14, (code >> 8) & 0xFF, code & 0xFF))
    if ecu.handle_request(payload, attempts=1):
        print("clear request accepted for 0x%04X" % code)
    else:
        print("clear failed:", ecu.errors)
```

### 6.4 Template D — raw request escape hatch

Use for Yamaha/Honda, advanced services, or reverse-engineering. Pass payload only.

```python
from kwp2000 import KWP2000


def raw_request(payload, attempts=1):
    if not ecu.connected:
        print("not connected")
        return None
    if not ecu.handle_request(payload, attempts=attempts):
        print("raw request failed: code=%s nrc=%s errors=%s" % (
            ecu.last_response_code, ecu.last_nrc, ecu.errors))
        return None
    return ecu.last_response_payload


# Example: read data by local identifier 0x21, local id 0x08 (Suzuki sensor block).
reply = raw_request(b"\x21\x08")
if reply is not None:
    print("reply:", " ".join(["%02X" % b for b in reply]))

# Yamaha/Honda: only raw requests are supported; label the output as raw.
# reply = raw_request(b"\x02")
```

### 6.5 Template E — Suzuki dealer mode

```python
from kwp2000 import KWP2000

DEALER_PIN = 4   # set to your optocoupler control pin; None disables this feature

if DEALER_PIN is not None:
    if not ecu.enable_dealer_mode(DEALER_PIN):
        print("dealer mode is Suzuki-only:", ecu.errors)
    else:
        print("dealer mode before:", ecu.dealer_mode)
        ecu.set_dealer_mode(True)
        print("dealer mode now:", ecu.dealer_mode)
        ecu.set_dealer_mode(False)
```

### 6.6 Template F — debug and diagnostics

```python
from kwp2000 import KWP2000

# Pick one: default lifecycle messages or verbose frame dumps.
ecu.enable_debug(level=KWP2000.DEBUG_DEFAULT)
# ecu.enable_debug(level=KWP2000.DEBUG_VERBOSE)

ecu.print_status(min_interval_ms=0)   # 0 forces the print
ecu.print_sensors()
ecu.print_last_response()

print("frame options:", ecu.frame_options)
print("errors:", ecu.errors)
print("last response code:", ecu.last_response_code)
print("last NRC:", ecu.last_nrc)
```

---

## 7. Prioritized pitfalls and decision rules

### P0 — Safety and protocol

| # | Rule | Why / correct pattern |
| --- | --- | --- |
| 1 | Never connect an MCU pin to the 12 V K-line. | Use L9637/MC33660/MC33199 or equivalent; common ground. |
| 2 | Do not use Arduino/C++ API names or return conventions. | The port returns `bool`/`list`/`None`, not Arduino negative numbers. `kwp2000.py` is authoritative. |
| 3 | `tx_pin` is mandatory even with an external UART. | `connect()` raises `RuntimeError` without it. |
| 4 | Pass payload only to `handle_request()`. | The library adds format, addresses, length, and checksum. Payload length must be 1..255. |
| 5 | Do not call the library from an ISR, thread, or async task. | It is fully blocking and not reentrant. |
| 6 | `clear_trouble_codes(0)` clears DTC 0, not all codes. | Use `clear_trouble_codes()` or `clear_trouble_codes(None)` for clear-all. |
| 7 | `keep_alive()` returning `True` does not mean its request succeeded. | It returns the connection flag. The next call may expire the session. |
| 8 | `request_sensors()` returning `True` does not guarantee all Kawasaki values decoded. | Kawasaki tracks only `handle_request()` success; check `ERR_HEADER` when values matter. |
| 9 | Never assume auto-keep-alive or auto-reconnect. | Neither exists; implement both in the program. |
| 10 | Never treat `None` as zero or as an error by itself. | `None` means "no value yet"; failed refreshes keep the old value. Guard all formatting. |

### P1 — Timing and blocking

| # | Rule | Why / correct pattern |
| --- | --- | --- |
| 11 | Budget for worst-case blocking (see §5.4). | Kawasaki `request_sensors()` can block ~44-45 s on a silent bus. Configure the watchdog accordingly. |
| 12 | Call `keep_alive()` at least every 2 s; every 0.5-1.5 s is safer. | The link expiry is 2000 ms. Avoid unrelated blocking work >1.5 s. |
| 13 | `keep_alive()` values >2000 clamp to 1500 and set `ERR_USER`. | Pass `0` for the default; do not pass >2000. |
| 14 | The 2000 ms response timeout is inter-byte. | A slow byte stream can extend listening beyond 2 s. |
| 15 | Feed the watchdog before/after long calls. | There is no background task to feed it during a blocking request. |

### P2 — API semantics and diagnostics

| # | Rule | Why / correct pattern |
| --- | --- | --- |
| 16 | `handle_request()` does not check or set `connected`. | Call `connect()` first; do not infer session state from a raw response. |
| 17 | `check_response=False` still sets error flags and `last_nrc`. | It accepts any complete checksum-valid frame, including a negative response. Use only for diagnostics. |
| 18 | `last_response_code` is the primary response classifier. | `None` initially; `0` positive; `-1` no/bad; `-2..-8` NRC; `-9` unexpected. |
| 19 | Error flags accumulate until `reset_error()` or a fresh `connect()`. | Snapshot `before = ecu.error_flags` and compare after a call. |
| 20 | `connect()` while already connected returns `True` without resetting errors. | Call `reset_error()` explicitly if a clean slate is required. |
| 21 | Disconnected DTC calls return `[]`/`False` + `ERR_USER` before validating arguments. | Do not rely on `ValueError` while disconnected. |
| 22 | `disconnect()` preserves sensor values and error flags. | Response frames are cleared, but old readings/flags remain. |
| 23 | Kawasaki `tps`/`speed` are raw bytes for one-byte replies; two-byte replies use the §4.5 formulas. Suzuki `rpm` is a float. | Use the brand-specific table in §4.5; never reuse Suzuki formulas for Kawasaki. |
| 24 | `use_imperial()`/`use_metric()` change only returned values. | `sensors` applies the same conversion; stored readings are unchanged. |
| 25 | `debug=` alone does not enable output. | `debug_level` defaults to `DEBUG_NONE`; call `enable_debug(level=...)` or pass `debug_level > 0`. |
| 26 | `print_status()` can be silent in the first 5 s. | `_last_status_print` starts at 0; pass `min_interval_ms=0` to force. `print_sensors()` sets `ERR_USER` when all values are `None`. |
| 27 | UART baudrate is forced to 10400. With `uart_id`, the UART is deinitialized before the waveform and recreated after it; otherwise it is reinitialized. | If `uart_kwargs` contains `"pins"`, `tx`/`rx` are not added; use `uart_id` as a fallback. |
| 28 | Yamaha/Honda: sensor decoding is not implemented. | `request_sensors()` returns `False` + `ERR_UNSUPPORTED`; use `handle_request()` and label raw output. |
| 29 | Dealer mode is Suzuki-only; `dealer_mode` is `None` until enabled. | Enable first; check `dealer_mode is not None`. |
| 30 | Do not use private fields or internal constants (`_connected`, `_sensors`, `_last_sensors`, `_response_header_len`, `ISO_MAX_DATA`) as public API. | Use the documented properties/methods. |

---

## 8. Validation checklist for generated programs

### 8.1 Before writing

- [ ] Board and MicroPython firmware confirmed; UART peripheral and pins known.
- [ ] Interface IC is L9637/MC33660/MC33199 or equivalent; common ground planned.
- [ ] Brand confirmed: Suzuki/Kawasaki for full features; Yamaha/Honda raw only.
- [ ] `tx_pin`/`rx_pin` match the UART configuration.
- [ ] Watchdog timeout is compatible with the worst-case blocking in §5.4, or feeds are planned.
- [ ] Suzuki dealer mode (if used) has the optocoupler and ~330 ohm resistor.

### 8.2 Static/code review

- [ ] Imports are `from machine import UART` and `from kwp2000 import KWP2000`.
- [ ] No invented methods, properties, constants, or keyword arguments.
- [ ] Constructor supplies `uart`/`uart_id`, `brand`, `tx_pin`, and preferably `rx_pin`.
- [ ] UART is at 10400 baud; the library forces it on reinit.
- [ ] `connect()` return value is checked; failure prints `ecu.errors`.
- [ ] `keep_alive()` is called in every connected loop.
- [ ] `request_sensors()`/DTC/raw return values are checked.
- [ ] Every sensor value is guarded against `None` before formatting/arithmetic.
- [ ] Brand-specific sensor semantics are used (Kawasaki TPS/speed raw or 2-byte reply; Suzuki RPM float).
- [ ] Empty DTC list is classified with `last_response_code`/new error bits.
- [ ] Clear-all uses `clear_trouble_codes()`; single-byte clear uses an int; `0` is not clear-all.
- [ ] Raw payloads contain only service/data bytes; header/checksum are left to the library.
- [ ] `disconnect()` is in a `finally` block.
- [ ] Reconnect logic has a delay and a retry limit; no tight failure loop.
- [ ] No async/threads/ISR calls into the library.
- [ ] Debug is off or minimal in production; `DEBUG_VERBOSE` only for diagnosis.
- [ ] Formatting does not assume every sensor is `int` or `float`.

### 8.3 Runtime/hardware

- [ ] Import succeeds and `kwp2000.__version__` prints.
- [ ] `connect()` returns `True`; debug shows "Initialize K-line" and "ECU connected".
- [ ] `frame_options` shows a coherent length-byte/address/timing configuration.
- [ ] Sensor values are plausible; `None` fields match the brand (e.g. Kawasaki `clutch`/`stps`/`voltage`).
- [ ] Polling stays connected for at least 60 s with `keep_alive()`.
- [ ] Disconnect/reconnect works; sensor values persist as documented.
- [ ] Unplugging/replugging the K-line triggers the explicit reconnect path without a crash.
- [ ] DTC empty/failed cases are distinguishable in logs.
- [ ] The watchdog does not reset during normal or worst-case operations.
- [ ] `disconnect()` returns and the program exits or loops safely.

---

## 9. Testing and verification

### 9.1 Host test suite `[V]`

Run from the repository root:

```sh
python -m unittest discover -s micropython/tests -p "test_*.py" -v
```

98 tests pass on plain CPython with a fake UART, fake ECU, fake clock, and fake pin. No hardware
or third-party packages are required.

### 9.2 What the host tests do not cover

- Real wiring, voltage levels, or the 12 V interface.
- Real `machine.Pin` creation from an integer.
- Real `machine.UART` creation/reinitialization and the `uart_id`-only creation path.
- `mip` installation.
- Real 10400-baud timing and watchdog behavior.
- Yamaha/Honda protocol behavior.

### 9.3 Hardware smoke test `[P]`

1. Flash `kwp2000.py` (and optionally [`examples/basic_working.py`](examples/basic_working.py)).
2. Confirm import and `kwp2000.__version__`.
3. Run `connect()` with `DEBUG_DEFAULT`; expect "Initialize K-line" and "ECU connected".
4. Run `request_sensors()`; confirm plausible values and brand-appropriate `None` fields.
5. Run `read_trouble_codes()`; confirm the DTC list and failure classification.
6. Leave the program idle for at least 10 s and confirm keep-alive traffic/status stays connected.
7. Disconnect, reconnect, and confirm sensor values persist until refreshed.
8. Run a 60 s polling loop and confirm no watchdog resets.

### 9.4 Self-check generated code without hardware

- Compare every API name against §4 and [`kwp2000.py`](kwp2000.py).
- Compile the program with CPython for syntax: `python -m py_compile program.py`.
- Check the program against the checklist in §8.

---

## 10. Known limitations

- **Yamaha and Honda:** raw requests only; no sensor decoding.
- **Access timing parameter (ATP):** not implemented; `_timing` is informational and does not
  change timing. Extended-timing key bytes set `ERR_UNSUPPORTED`.
- **DTC decoding:** returns raw 16-bit codes; no manufacturer DTC strings, descriptions, or
  P-code formatting.
- **No auto-reconnect, auto keep-alive, async, threads, callbacks, interrupts, or context
  manager.**
- **Sensor persistence:** values are not reset by `disconnect()` or `connect()`.
- **Fixed timing:** the K-line timings are fixed; only the keep-alive interval is adjustable.
- **Brand-specific sensors:** Kawasaki has no clutch, STPS, or voltage readings.
- **Private/internal:** `ISO_MAX_DATA` (an unused module-level constant) and underscore-prefixed
  fields such as `_last_sensors` and `_response_header_len` are not public API.

---

## 11. Appendices

### Appendix A — one-page cheat sheet

```python
# Install: mpremote mip install github:mahirtaherosl-eng/Keyword-Protocol-2000
# Import
from machine import UART
from kwp2000 import KWP2000

# Construct
uart = UART(2, baudrate=10400, tx=17, rx=16)
ecu = KWP2000(uart, "suzuki", tx_pin=17, rx_pin=16)

# Or let the library create the UART
# ecu = KWP2000(None, "suzuki", tx_pin=17, rx_pin=16, uart_id=2)

# Lifecycle
ok = ecu.connect()
ecu.keep_alive()                 # call repeatedly while connected
ecu.request_sensors()            # Suzuki: 1 request; Kawasaki: 7
ecu.read_trouble_codes()         # list[(code, status)]
ecu.clear_trouble_codes()        # clear all
ecu.clear_trouble_codes(0x12)    # clear one byte
ecu.handle_request(b"\x21\x08")  # payload only
ecu.disconnect()

# Sensors (None until refreshed)
ecu.gps; ecu.clutch; ecu.rpm; ecu.speed; ecu.tps
ecu.stps; ecu.iap; ecu.iat; ecu.ect; ecu.voltage
ecu.sensors                      # dict, unit conversion applied

# Units
ecu.use_imperial(); ecu.use_metric()

# Diagnostics
ecu.connected; ecu.errors; ecu.error_flags; ecu.has_error
ecu.last_response; ecu.last_response_payload
ecu.last_response_code; ecu.last_nrc; ecu.frame_options
ecu.reset_error()

# Debug
ecu.enable_debug(level=KWP2000.DEBUG_DEFAULT)
ecu.print_status(min_interval_ms=0)
ecu.print_sensors()
ecu.print_last_response()

# Suzuki dealer mode
ecu.enable_dealer_mode(4)
ecu.set_dealer_mode(True)
ecu.dealer_mode
```

### Appendix B — update this document when...

- Constructor or method signatures/defaults change.
- Class/module constants or error bits change.
- Sensor formulas, offsets, or units change.
- Timing constants (`ISO_*`, `KEEP_ALIVE_MS`) or retry counts change.
- The `package.json`/mip install path changes.
- The module version changes (update the header and last-verified date).

### Appendix C — file map

| Path | Purpose |
| --- | --- |
| [`kwp2000.py`](kwp2000.py) | The entire library; source of truth. |
| [`README.md`](README.md) | Human-facing install, wiring, API and troubleshooting. |
| [`examples/basic_working.py`](examples/basic_working.py) | REPL example mirroring the Arduino sketch. |
| [`tests/test_kwp2000.py`](tests/test_kwp2000.py) | 98 host tests. |
| [`tests/fakes.py`](tests/fakes.py) | Fake UART/ECU/clock/pin. |
| [`../package.json`](../package.json) | `mip` manifest. |
| [`../LICENSE`](../LICENSE) | GPL-3.0 repository license. |

---

**License:** This port is distributed under the repository's GPL-3.0 license. The original
Arduino source files carry an MIT-style notice; MIT is compatible with GPL-3.0.
