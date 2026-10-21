# Bluetooth Android logger (ESP32-C6)

An ESP32-C6 running MicroPython polls every KWP2000 sensor value and streams it
over **Bluetooth Low Energy** as newline-delimited JSON. An Android app scans
for the board, shows a live dashboard, logs every sample to CSV, shares the
file, and can change the sensor polling interval from the phone.

```text
K-line (12 V) -> L9637 -> ESP32-C6 UART1 -> BLE GATT -> Android app (CSV)
```

## Folder layout

| Path | Purpose |
| --- | --- |
| `esp32_ble_logger.py` | ESP32-C6 program (BLE server + KWP2000 polling). |
| `android/` | Android Studio project (Kotlin, no BLE third-party libraries). |
| `tools/ble_receiver.py` | Optional PC receiver (needs `bleak`). |
| `tools/test_protocol.py` | Host tests for the JSON/chunking/control helpers. |

## Hardware and wiring

The program defaults to the **ESP32-C6** profile used by the WiFi dashboard
example: UART1, TX=GPIO19, RX=GPIO20, L9637 VCC=GPIO22. The standard
`ESP32_GENERIC_C6` MicroPython firmware includes BLE.

| L9637 pin | Connect to |
| --- | --- |
| 1 (RX output) | ESP32 GPIO20 (UART RX) |
| 4 (TX input) | ESP32 GPIO19 (UART TX) |
| 3 (VCC) | ESP32 GPIO22 (switched by the program) |
| 5 (GND) | Common ground |
| 7 (VS) | Switched 12 V from the bike |
| 6 (K-line) | Bike K-line |

> **Safety:** the K-line is a 12 V single-wire bus. Never connect an ESP32 pin
> directly to it or to 12 V. Use an L9637/MC33660/MC33199 interface and a
> common ground with the bike. GPIO22 sources the module's logic VCC; if the
> module draws more than a safe GPIO current (or has a regulator/LED), drive a
> MOSFET/load switch from GPIO22 instead.

C6 pin notes: UART0 (GPIO16/17) is the REPL on the generic firmware, GPIO10/11
are unusable on this board, and GPIO4/5/8/9/15 (strapping), 12/13 (USB) and
24-30 (SPI flash) should be avoided. If GPIO19/20 are unavailable, use a pair
that passes a plain loopback (for example 1/2 or 18/19) and update the config.

## ESP32: install and run

1. Flash an official `ESP32_GENERIC_C6` build. Confirm BLE is present:

   ```python
   import bluetooth
   print(bluetooth.BLE())
   ```

2. Install the library and the program (quote the paths because the example
   folder has spaces):

   ```sh
   mpremote mip install github:mahirtaherosl-eng/Keyword-Protocol-2000
   mpremote fs cp "micropython/examples/bluetooth android logger/esp32_ble_logger.py" :main.py
   ```

   Or copy `micropython/kwp2000.py` to `:lib/kwp2000.py` manually.

3. Reset the board. The serial console prints `BLE advertising as KWP2000-C6`.
   The program does not auto-start KWP2000 polling until a phone connects.

### Configuration

Edit the block at the top of `esp32_ble_logger.py`:

| Constant | Default | Meaning |
| --- | --- | --- |
| `BRAND` | `"kawasaki"` | `"kawasaki"` or `"suzuki"` |
| `DEVICE_NAME` | `"KWP2000-C6"` | BLE advertising name |
| `UART_ID`, `TX_PIN`, `RX_PIN` | `1, 19, 20` | K-line UART |
| `L9637_VCC_PIN` | `22` | Switched L9637 logic VCC |
| `POLL_INTERVAL_MS` | `2000` | Default poll period |
| `MIN/MAX_POLL_INTERVAL_MS` | `1000/10000` | Accepted phone range |
| `KEEPALIVE_INTERVAL_MS` | `1000` | Must stay below the 2 s link expiry |
| `SIMULATE` | `False` | Synthetic sensors; no UART/ECU |
| `DEBUG_LEVEL` | `DEBUG_NONE` | Set `DEBUG_DEFAULT` during bring-up |

**Bench test without a bike:** set `SIMULATE = True`, flash, and connect with
the Android app or the PC receiver. The BLE, JSON, control and CSV paths work
without an ECU.

**Blocking budget:** `request_sensors()` can block ~6.5 s (Suzuki) or
~44-45 s (Kawasaki) on a silent bus; `connect()` up to ~7.5 s and
`disconnect()` up to ~8.5 s. No watchdog is enabled. If you enable one, use a
timeout well above these values; it cannot be fed inside a library call.

## BLE protocol

| Item | UUID | Properties |
| --- | --- | --- |
| Service | `6b773230-2d4b-5750-8000-6b7770323030` | — |
| Sensor data | `6b773230-2d4b-5750-8001-6b7770323030` | NOTIFY |
| Control | `6b773230-2d4b-5750-8002-6b7770323030` | WRITE |
| CCCD | `00002902-0000-1000-8000-00805f9b34fb` | standard |

Advertising contains the flags and the complete 128-bit service UUID; the scan
response contains the name. There is no pairing or bonding.

### Sensor stream

The ESP32 sends one **newline-terminated JSON object per sample**. A BLE
notification is a *chunk*, not a message: its maximum size is `MTU - 3` (20
bytes at the default MTU 23, 244 when the phone negotiates 247). Receivers
must buffer chunks and split on `\n`.

```json
{"seq":42,"ms":123456,"gps":3,"clutch":null,"rpm":3125,"speed":48,"tps":14.2,"stps":null,"iap":101,"iat":26.5,"ect":81,"voltage":null}
```

- Fixed keys: `seq`, `ms`, then `gps, clutch, rpm, speed, tps, stps, iap, iat,
  ect, voltage`.
- `null` means "no value" (not zero). Kawasaki normally leaves `clutch`,
  `stps` and `voltage` null.
- Floats are rounded to two decimals. `seq` restarts at 1 after an ESP32
  reboot; `ms` is board uptime and wraps.
- The library keeps the previous value after a failed refresh, so use
  `seq`/`ms` to judge staleness.

Kawasaki two-byte speed/TPS replies are decoded by `kwp2000.py`: speed is
`(b2 * 100 + b3) / 2` km/h and TPS is `125 * (u16 - 201) / (405 - 201)` with
`u16 = (b2 << 8) | b3`. One-byte replies keep the raw byte.

### Update-interval control

Write one JSON line to the control characteristic:

```json
{"cmd":"interval","ms":2000}
```

The accepted range is 1000-10000 ms; values outside it are clamped. The ESP32
replies on the sensor characteristic with:

```json
{"ack":"interval","ms":2000,"ok":true}
```

Invalid commands return `ok:false` and an `error` string. ACK lines are never
logged to CSV or shown as sensor values. An interval command that arrives
during a blocking sensor poll is applied when the poll returns.

## Android app

### Build

Requirements: Android Studio (Koala or newer), JDK 17, Android SDK 34, and a
phone running Android 8.0+ with BLE.

1. Open the `android/` folder in Android Studio.
2. The Gradle wrapper **jar/scripts are intentionally not committed** (binary
   files). Android Studio can import the project from the pinned
   `gradle/wrapper/gradle-wrapper.properties` (Gradle 8.7). If your setup wants
   a wrapper, run `gradle wrapper --gradle-version 8.7` once, then use
   `./gradlew assembleDebug`.
3. Build the debug APK or install directly from Studio:

   ```sh
   adb install -r app/build/outputs/apk/debug/app-debug.apk
   ```

The app uses only AndroidX `core`/`activity`; there are no BLE or CSV
third-party libraries.

### Use

1. Tap **Scan**. On Android 12+ grant **Nearby devices**; on Android 11 and
   below grant location and enable location services.
2. Tap `KWP2000-C6` in the device list.
3. The dashboard updates once per poll. Choose **Update interval** (1, 2, 5 or
   10 s); the app writes the command and shows the ACK.
4. Tap **Start logging** to begin CSV capture, then **Stop logging**.
5. Tap **Share CSV** to export the file through any file-capable app.

If the BLE link drops unexpectedly, the app reconnects with a 1-30 s backoff
and re-sends the selected interval. The interval is stored in
`SharedPreferences` and restored after an app restart.

### CSV files

Files are stored in app-specific external storage, so no storage permission is
needed:

```text
/sdcard/Android/data/com.example.kwpblelogger/files/logs/kwp2000_YYYYMMDD_HHMMSS.csv
```

Header:

```text
timestamp_iso,seq,uptime_ms,gps,clutch,rpm,speed,tps,stps,iap,iat,ect,voltage
```

`timestamp_iso` is the phone's wall clock when the sample arrived; `uptime_ms`
is the ESP32 uptime. Null sensors are empty fields. Rows are flushed as they
are written.

Pull the logs with adb:

```sh
adb pull /sdcard/Android/data/com.example.kwpblelogger/files/logs/
```

## Optional PC receiver

Useful for separating ESP32 problems from Android problems:

```sh
pip install bleak
python "micropython/examples/bluetooth android logger/tools/ble_receiver.py" --duration 30
python "micropython/examples/bluetooth android logger/tools/ble_receiver.py" --csv pc_log.csv --interval 5000
```

Options: `--address` (skip scanning), `--scan-timeout`, `--duration` (0 =
until Ctrl-C), `--csv`, `--interval` (write a control command).

## Verification

From the repository root:

```sh
python -m py_compile "micropython/examples/bluetooth android logger/esp32_ble_logger.py"
python -m py_compile "micropython/examples/bluetooth android logger/tools/ble_receiver.py"
python "micropython/examples/bluetooth android logger/tools/test_protocol.py"
python -m unittest discover -s micropython/tests -p "test_*.py" -v
```

The protocol tests cover JSON keys/rounding, MTU chunking, advertising bytes,
control clamping/ACKs, and cross-file UUID/CSV consistency. The library suite
covers the Kawasaki two-byte speed/TPS formulas.

Hardware smoke test:

1. `SIMULATE = True`; confirm advertising on the serial console.
2. Connect with the app (or PC receiver) and watch samples arrive.
3. Change the interval and confirm the ACK and new cadence.
4. Log for 60 s, stop, share/open the CSV.
5. Set `SIMULATE = False`, turn on the ignition, and confirm plausible values.
6. Unplug the K-line mid-log: expect a pause up to ~44 s on a failed Kawasaki
   poll, then reconnect backoff; the ESP32 must not reset.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No device in the scan | Nearby devices permission, Bluetooth on, BLE firmware (`import bluetooth`), correct UUID, range. Android 11 and below also needs location services. |
| Connects but no values | ESP32 serial console: is `connect()` succeeding? Check ignition, K-line wiring, `BRAND`, L9637 VCC/VS. The app detail line should reach "Connected · MTU ...". |
| Values are null | Normal for Kawasaki `clutch`, `stps`, `voltage`; also null before the first successful poll. |
| Interval change has no effect | Wait for the ACK; the ESP32 applies it after any blocking poll. Values are clamped to 1-10 s. |
| Updates pause for tens of seconds | A failed Kawasaki `request_sensors()` blocks ~44 s. This is a library/bus limitation, not BLE. |
| Watchdog resets | Disable it or use >60 s; it cannot be fed inside a library call. |
| ESP32 resets/hangs | Keep the CPU frequency at the default (>=80 MHz); do not enable WiFi in this example. |
| CSV not visible in a file manager | It is app-specific storage. Use **Share CSV** or `adb pull`. |
| Share fails | Install a file-capable target app; the app uses FileProvider, never `file://`. |
| Decimal commas in a spreadsheet | The app writes `Locale.US` numbers; set the spreadsheet import locale. |

## Limitations

- One BLE central at a time; no pairing, bonding or encryption.
- No foreground service: logging pauses if Android kills the app in the
  background.
- Sensor values kept after a failed refresh can be stale; use `seq`/`ms`.
- Yamaha and Honda remain raw-request-only in the library.
- The example is configured for the ESP32-C6; other boards need UART/pin edits.
