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
(GPS raw), RPM, Speed (raw byte), and TPS (raw byte). Join the AP and open the
IP printed at boot (normally `http://192.168.4.1/`); `localhost` on the phone
refers to the phone itself. The AP has no internet access; if the phone warns
about this, choose to stay connected.

The example defaults to the **ESP32-C6**: UART1, TX=GPIO10, RX=GPIO11. Wire
GPIO10 to the interface TXD, GPIO11 to the interface RXD, and share a ground.
On the generic C6 firmware UART0 (GPIO16/17) is reserved for the REPL and
there is no normal UART2, so the classic UART2/17/16 profile fails with
`ESP_FAIL`. For other boards, edit the config block:

| Board | `UART_ID` | `TX_PIN` | `RX_PIN` |
| --- | --- | --- | --- |
| ESP32-C6 (default) | 1 | 10 | 11 |
| ESP32 classic / WROOM | 2 | 17 | 16 |
| ESP32-WROVER / PICO | 2 | 25 | 26 |

On the C6, GPIO18/19 or GPIO22/23 are alternatives if GPIO10/11 are not
exposed. Edit `SSID` and `PASSWORD` at the top as well.

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
