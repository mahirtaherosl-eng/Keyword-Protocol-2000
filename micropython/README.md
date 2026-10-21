# KWP2000 for MicroPython

A MicroPython port of the [KWP2000 Arduino library](../README.md). It speaks
KWP2000 / ISO 14230 over a 12 V K-line to motorbike ECUs.

| Brand | Status |
| ----- | ------ |
| Suzuki (SDS) | Supported |
| Kawasaki (KDS) | Supported |
| Yamaha (YDS) | Stub (raw requests only) |
| Honda (HDS) | Stub (raw requests only) |

The port is a single file, [`kwp2000.py`](kwp2000.py), with no third-party
dependencies.

> **AI coding agents:** see [`AI_HANDOVER.md`](AI_HANDOVER.md) for exact API semantics,
> timing/blocking rules, runnable program templates, and a validation checklist.

## Installation

Using [`mip`](https://docs.micropython.org/en/latest/reference/packages.html)
from the root manifest of the `mahirtaherosl-eng/Keyword-Protocol-2000` fork:

```sh
mpremote mip install github:mahirtaherosl-eng/Keyword-Protocol-2000
```

This installs `kwp2000.py` into the board's `lib` directory.

Manual copy:

```sh
mpremote fs cp micropython/kwp2000.py :lib/kwp2000.py
```

Older `mip` versions that do not resolve relative manifest URLs can install the
file directly:

```sh
mpremote mip install github:mahirtaherosl-eng/Keyword-Protocol-2000/micropython/kwp2000.py
```

## Wiring

The K-line is a 12 V single-wire bus. Use a level/interface IC such as the
**L9637**, **MC33660** or **MC33199** between the MCU and the bike. Connect the
MCU ground, the interface ground and the bike ground together.

![L9637 wiring](../extras/Images/l9637_wiring.png)

On the MCU side the library needs:

- `tx_pin` — the UART TX pin connected to the interface. It is also driven as a
  plain GPIO during the fast-init waveform, then the UART is reattached.
- `rx_pin` — the UART RX pin connected to the interface.
- `uart` — a `machine.UART` object at 10400 baud (the library reinitializes it
  at 10400 after the waveform).

The ESP32 example uses UART2 on TX GPIO17 / RX GPIO16. Adjust the pins for your
board.

### Suzuki dealer mode (optional)

Dealer mode requires an optocoupler and a ~330 ohm resistor between the MCU and
the bike's dealer pin:

![Dealer mode wiring](../extras/Images/dealer_mode.png)

Pass the MCU pin to `enable_dealer_mode()`; it is only available on Suzuki.

## Quick start

```python
from machine import UART
from kwp2000 import KWP2000

uart = UART(2, baudrate=10400, tx=17, rx=16)
ecu = KWP2000(uart, "suzuki", tx_pin=17, rx_pin=16)
ecu.enable_debug(level=KWP2000.DEBUG_DEFAULT)

if ecu.connect():
    if ecu.request_sensors():
        print("RPM:", ecu.rpm)
        print("Speed:", ecu.speed)
        print("Coolant:", ecu.ect)
    print("DTCs:", ecu.read_trouble_codes())
    ecu.disconnect()
```

`connect()` blocks for roughly 1.1 seconds while it generates the fast-init
waveform and performs the handshake. `disconnect()` sends stop communication
and blocks for up to about two seconds.

While connected, call `keep_alive()` regularly (for example every loop). The
ECU connection expires after 2 seconds without a valid response.

## API

### Constructor

```python
KWP2000(uart, brand, tx_pin=None, rx_pin=None, *,
        uart_id=None, uart_kwargs=None, debug=None,
        debug_level=DEBUG_NONE)
```

- `uart` — a `machine.UART`-like object, or `None` when `uart_id` is supplied
  (the UART is then created on `connect()`).
- `brand` — `KWP2000.SUZUKI`, `KWP2000.KAWASAKI`, `KWP2000.YAMAHA`,
  `KWP2000.HONDA`, or a case-insensitive string (`"suzuki"`).
- `tx_pin` / `rx_pin` — pin numbers or `machine.Pin` objects. `tx_pin` is
  required to generate the fast-init waveform.
- `uart_id` — UART peripheral number used if `uart` is `None` or if reattaching
  the existing UART fails.
- `uart_kwargs` — extra keyword arguments passed to `uart.init()` on every
  reinitialization (for example `{"timeout": 10, "rxbuf": 256}`).
- `debug` — a stream with `write()`; defaults to `sys.stdout` when enabled.
- `debug_level` — `DEBUG_NONE` (default), `DEBUG_DEFAULT` or `DEBUG_VERBOSE`.

### Constants

```python
KWP2000.SUZUKI, KWP2000.KAWASAKI, KWP2000.YAMAHA, KWP2000.HONDA
KWP2000.DEBUG_NONE, KWP2000.DEBUG_DEFAULT, KWP2000.DEBUG_VERBOSE
KWP2000.READ_TOTAL, KWP2000.READ_ONLY_ACTIVE, KWP2000.READ_ALL
KWP2000.ERR_START, KWP2000.ERR_STOP, KWP2000.ERR_TIMEOUT, ...
```

### Connection and communication

| Method | Description |
| ------ | ----------- |
| `connect()` | Fast-init waveform + start communication. Returns `True` on success or if already connected. |
| `disconnect()` | Sends stop communication, waits for P3 max, marks disconnected. Returns `False` if the stop request failed. |
| `request_sensors()` | Requests and decodes the brand's sensor block. Returns `True` only if every request succeeded. |
| `keep_alive(interval_ms=0)` | Sends tester-present (Suzuki/Yamaha/Honda) or a GPS request (Kawasaki) when needed. Returns `False` if disconnected/expired. |
| `handle_request(payload, attempts=3, check_response=True)` | Raw request escape hatch. The header and checksum are added. `attempts` is clamped to 1..3. |
| `read_trouble_codes(which=READ_ONLY_ACTIVE)` | Returns a list of `(code, status)` tuples. `status` is `None` for 2-byte records and an int for `READ_ALL`. |
| `clear_trouble_codes(code=None)` | `None` clears all (`14`); an integer sends `14 <code>` (one byte, like the Arduino library). |

Protocol failures return `False` (or an empty list) and set error flags.
Invalid arguments raise `ValueError`. A missing UART/pin raises `RuntimeError`.

### Sensor properties

Values are `None` until the first successful request and keep their last value
after a failed refresh.

| Property | Metric unit | Notes |
| -------- | ----------- | ----- |
| `gps` | gear | raw value |
| `clutch` | raw | Suzuki only |
| `rpm` | rpm | full-width value (not truncated to a byte) |
| `speed` | km/h | mph when `use_imperial()` is active |
| `tps` | % | calculated |
| `stps` | raw | Suzuki only |
| `iap` | kPa | calculated |
| `iat` | °C | °F when imperial |
| `ect` | °C | °F when imperial |
| `voltage` | V | Suzuki only |
| `sensors` | dict | all of the above, with unit conversion applied |

`use_imperial()` and `use_metric()` only change the values returned by the
properties; they never modify the stored readings, so repeated reads are
stable and switching back restores the metric values.

Kawasaki `speed` and `tps` decode a second data byte when the ECU sends one.
A one-byte reply keeps the raw value; a two-byte reply uses
`(b2 * 100 + b3) / 2` km/h for speed and
`125 * (u16 - 201) / (405 - 201)` for TPS, where `u16` is the big-endian
`(b2 << 8) | b3` value (`00 DE` = 222).

### Status, errors and debug

| Member | Description |
| ------ | ----------- |
| `connected` | `True` while the session is open. |
| `brand`, `brand_name` | Normalized brand constant/name. |
| `frame_options` | `{"use_length_byte", "use_addresses", "timing"}`. |
| `error_flags` | Bitmask of accumulated errors. |
| `has_error` | `True` if any error bit is set. |
| `errors` | Tuple of error names (for example `("timeout",)`). |
| `reset_error()` | Clears the error bitmask. |
| `last_response` | Last complete frame, including header and checksum. |
| `last_response_payload` | Last payload, without header/checksum. |
| `last_response_service` | First payload byte (response service id). |
| `last_response_code` | `0` positive, `-1` no/bad response, `-2..-8` negative response codes, `-9` unexpected. |
| `last_nrc` | Raw negative response code, if any. |
| `enable_debug(stream=None, level=DEBUG_DEFAULT)` | Enable debug output. |
| `set_debug_level(level)`, `disable_debug()` | Change/disable debug output. |
| `print_status(min_interval_ms=5000)` | Print connection/frame/error status. |
| `print_sensors()` | Print the decoded sensor block. |
| `print_last_response()` | Hex-dump the last complete frame. |

### Suzuki dealer mode

```python
ecu.enable_dealer_mode(pin)   # Suzuki only
ecu.set_dealer_mode(True)
print(ecu.dealer_mode)        # None if not enabled
```

## Example

[`examples/basic_working.py`](examples/basic_working.py) mirrors the Arduino
`basic_working.ino` sketch. After copying it and `kwp2000.py` to the board, run
it from the REPL and use:

| Key | Action |
| --- | ------ |
| `i` | connect |
| `d` | toggle dealer mode (if enabled) |
| `t` | read trouble codes |
| `s` | request and print sensors |
| `p` | print status |
| `c` | disconnect |
| `?` | help |

### WiFi dashboard (ESP32, Kawasaki)

[`examples/kawasaki_wifi_dashboard.py`](examples/kawasaki_wifi_dashboard.py)
starts a WiFi access point and serves a phone-friendly dashboard with Gear
(GPS raw), RPM, Speed, and TPS. One-byte Kawasaki speed/TPS replies are raw
values; replies with a second data byte are decoded. Join the AP and open the
IP printed at boot (normally `http://192.168.4.1/`); `localhost` on the phone
refers to the phone itself. The AP has no internet access; if the phone warns
about this, choose to stay connected.

The example defaults to the **ESP32-C6**: UART1, TX=GPIO19, RX=GPIO20, with
the L9637 logic VCC switched from GPIO22. Wire GPIO19 to the L9637 TX input
(pin 4), GPIO20 to its RX output (pin 1), GPIO22 to its VCC (pin 3), VS
(pin 7) to switched 12 V, and share a ground. GPIO22 is driven high while the
program runs and low on exit. UART0 (GPIO16/17) is reserved for the REPL on
the generic C6 firmware; GPIO10/11 are not usable on this board (a loopback
returned 0x00). For other boards, edit the config block:

| Board | `UART_ID` | `TX_PIN` | `RX_PIN` |
| --- | --- | --- | --- |
| ESP32-C6 (default) | 1 | 19 | 20 |
| ESP32 classic / WROOM | 2 | 17 | 16 |
| ESP32-WROVER / PICO | 2 | 25 | 26 |

If GPIO19/20 are unavailable on your C6 board, use a pair that passes the
plain loopback (for example 1/2 or 18/19); keep GPIO22 for the switched VCC.
Edit `SSID` and `PASSWORD` at the top as well. The page also has a scrolling
**Console** panel that mirrors the script's serial output; set `DEBUG_LEVEL`
to `KWP2000.DEBUG_DEFAULT` or `DEBUG_VERBOSE` to include KWP2000 debug
messages.

#### K-line troubleshooting

If `connect()` fails, the Console shows a line such as
`connect failed: code=-1 nrc=None errors=('start', 'echo')`.

| Error names | Meaning | Check |
| --- | --- | --- |
| `start` + `code=-1` | No valid ECU response | K-line pin, module TX/RX, pull-up, ignition, fast-init |
| `echo` | Echo bytes do not match what was sent | Module RX output -> ESP32 RX, module inversion, floating RX |
| `checksum`, `header`, `from`, `to` | Bytes arrive but the frame is malformed | Baud/timing, ground, K-line noise |
| `config`, `unsupported` | Key bytes/protocol rejected | Bike model/protocol compatibility |

Set `DEBUG_LEVEL = KWP2000.DEBUG_VERBOSE` to log every byte. With the ignition
on, the first attempt should show `Sending: 81 11 F1 81 04` followed by five
`Echo:` lines. Matching echoes mean the local RX path works; random or
inverted echoes mean the module/ESP32 RX wiring is wrong. One `Echo: 00` or
no echoes at all means the local TX/RX/logic path is not working; five
matching echoes mean the module path works and the ECU/bike is the next
suspect.

Two loopback checks isolate the ESP32 side. Disconnect the module from
GPIO19/GPIO20 and jumper the two pins together:

```python
import time
from machine import UART
u = UART(1, baudrate=10400, tx=19, rx=20)
u.write(b"\x55")
time.sleep_ms(20)
print(u.read())      # expect b'U'
```

If this fails, the UART/pins are the problem; use a pair that passes the
loopback (for example 1/2 or 18/19) and update `TX_PIN`/`RX_PIN`. GPIO22 is
reserved for the switched L9637 VCC. If it passes, run the dashboard with the
jumper still in place: the verbose log should show five matching `Echo:`
lines after `Sending: 81 11 F1 81 04`, proving the fast-init/recreate path.
Remove the jumper and reconnect the module afterward.

L9637/MC33660 wiring (datasheet pin functions, not module header labels):

| L9637 pin | Function | Connect to |
| --- | --- | --- |
| 4 | TX input | ESP32 GPIO19 |
| 1 | RX output | ESP32 GPIO20 |
| 5 | GND | common ground |
| 3 | VCC (3.0-7.0 V) | ESP32 GPIO22 (switched) |
| 7 | VS (4.5-36 V) | switched 12 V |
| 6 | K-line | bike K-line |

The L9637 needs **both** supplies: logic VCC on pin 3 and battery VS on pin 7.
Here VCC is switched from ESP32 GPIO22 (high while the program runs, low on
exit) and VS is switched 12 V. Powering only one of them leaves the K-line
driver or the logic interface dead. GPIO22 directly sources the module's VCC;
the L9637 itself draws little, but if the module adds a regulator, LED, or
other load, drive a MOSFET/load switch from GPIO22 instead. With VCC = 3.3 V
the logic levels are directly ESP32-compatible. If the module runs VCC at
5 V, its TX high threshold is about 3.5 V (marginal for a 3.3 V ESP32) and
its RX output is 5 V; use a level shifter or a 3.3 V module.

Module headers often label "TX"/"RX" from either the MCU or the module side;
verify the direction against the module schematic. The K-line needs a
510 ohm-5 kohm pull-up to 12 V (usually in the bike or module).

With the module powered and the K-line idle, measure the module RX output: it
should idle at the module's logic VCC (~3.3 V). A 0 V/floating RX output or a
5 V logic supply points at the module/wiring rather than the bike.

Bike-side checks:

- Find the K-line pin in the Versys 650 service manual; do not identify it by
  12 V alone, because other connector pins are also at 12 V.
- With the bike off, check continuity from the module K pin to that connector
  pin.
- Do not connect K to battery +12 V.
- Confirm the ESP32, module, and bike grounds are common.

With a scope or logic analyzer during connect, GPIO19 should show ~1 s high,
~25 ms low, ~25 ms high, then the request bytes. The module K pin and the bike
K-line should show the same wake-up pulse and request bytes. If the K-line
never shows them, the problem is before the bus (module TX pin/wiring); if it
does and the ECU stays silent, check the bike pin, ECU state, and fast-init
timing. Power-cycle the bike and retry once. If possible, run the C++
`basic_working` sketch with the same interface and bike to separate a
port/timing issue from a hardware issue.

### Bluetooth Android logger (ESP32-C6)

[`examples/bluetooth android logger/`](examples/bluetooth%20android%20logger/README.md)
streams all sensor values over BLE as newline-delimited JSON. The folder
includes an Android Studio app (Kotlin) that scans and connects, shows a live
dashboard, logs CSV, shares the file, and changes the ESP32 poll interval from
the phone. The ESP32 advertises as `KWP2000-C6`; C6 wiring matches the WiFi
dashboard (UART1, TX=GPIO19, RX=GPIO20, L9637 VCC=GPIO22). Set
`SIMULATE = True` to test the BLE/app path without an ECU. The folder README
documents the UUIDs, JSON schema, control command, build steps and
troubleshooting.

## Differences from the Arduino library

- Pythonic, snake_case API with properties instead of `get*()` methods, and
  blocking `connect()` / `disconnect()` instead of polled phase machines.
- RPM and speed are not truncated to 8 bits.
- Imperial conversion is read-only (the C++ version mutates stored values and
  compounds the conversion on every getter call).
- A frame with a bad checksum is rejected; the C++ version could accept a
  positive response with a bad checksum.
- The "separate length byte without address bytes" frame layout is fixed.
- Response decoding is bounds-checked and the response length-byte style is
  taken from the format byte on every frame.
- DTCs are returned as structured tuples; raw bytes stay available.
- `None` means "no value yet"; `clear_trouble_codes(None)` clears all codes
  (the C++ API used `0x00` for "clear all", which conflicts with a valid code).
- Yamaha and Honda return `False` + `ERR_UNSUPPORTED` for sensor requests;
  raw `handle_request()` remains available.
- The access-timing-parameter (ATP) functions are not implemented, matching the
  current Arduino build (they are commented out there).
- The `model` constructor argument was dropped because only `NONE` exists in
  the C++ version.
- The UART is never `deinit()`ed; after the fast-init waveform it is
  reinitialized with `tx`/`rx` so MicroPython can reattach the peripheral.
- Computed sensor values use Python true division and are floats; the C++
  version stores several of them in `uint8_t` fields and truncates.

## Testing

The host suite runs on plain CPython with a fake UART, fake ECU and fake clock;
no hardware or third-party packages are needed:

```sh
python -m unittest discover -s micropython/tests -p "test_*.py" -v
```

## Troubleshooting

- **No response / `ERR_START`** — check the interface wiring, the common ground,
  the 12 V supply, that the K-line pin is correct, and that the UART is at
  10400 baud.
- **`RuntimeError: UART.init() failed to reattach ...`** — some ports do not
  accept `tx`/`rx` in `UART.init()`. Pass `uart_kwargs={"pins": ...}` for such
  ports, or construct the UART with `uart_id`.
- **`ERR_ECHO`** — the interface does not reflect transmitted bytes back. This
  is advisory; check the wiring if it happens together with communication
  failures.
- **Interactive commands in the example do not work** — some USB-CDC firmware
  builds cannot poll `sys.stdin`. The main loop still calls `keep_alive()`;
  use a UART REPL or adapt the input handling.
- **Connection expires after two seconds** — call `keep_alive()` at least every
  two seconds.
- **Yamaha / Honda** — sensor decoding is not implemented; only raw requests
  are usable.

## License

This port is distributed under the repository's **GPL-3.0** license. The
original Arduino source files carry an MIT-style permission notice; MIT is
compatible with GPL-3.0. See [`../LICENSE`](../LICENSE).
