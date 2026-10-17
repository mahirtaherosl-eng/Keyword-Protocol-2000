"""ESP32 Kawasaki KWP2000 WiFi dashboard.

The ESP32 hosts a WiFi access point and serves a phone-friendly page with
Gear, RPM, Speed and TPS from a Kawasaki ECU. The page refreshes once per
second from a cached snapshot; the ECU is polled about every two seconds.

Wiring
------
ESP32-C6 defaults: UART1 TX=GPIO10 -> L9637 TXD, RX=GPIO11 <- L9637 RXD,
with a common ground to the bike. Never connect an ESP32 pin directly to the
12 V K-line. Board profiles (also in the configuration block):
  ESP32-C6 (default)   UART1  TX=GPIO10  RX=GPIO11
  ESP32 classic/WROOM  UART2  TX=GPIO17  RX=GPIO16
  ESP32-WROVER/PICO    UART2  TX=GPIO25  RX=GPIO26  (16/17 are PSRAM)
On the C6, UART0/GPIO16-17 is reserved for the REPL in the generic firmware
and there is no normal UART2; do not use those pins.

Phone
-----
Join the access point printed at boot, then open the IP printed there
(normally http://192.168.4.1/). "localhost" on the phone refers to the phone
itself, not the ESP32. The AP has no internet access; if the phone warns
about this, choose to stay connected. The page also mirrors this script's
console output in a scrolling Console panel below the table; set DEBUG_LEVEL
above DEBUG_NONE to include KWP2000 debug messages.

Kawasaki values
---------------
These are raw ECU bytes (verified in kwp2000.py and the C++ source):
  gps   - Gear Position Sensor raw byte (not a gear number)
  rpm   - computed by the library as ((b2*255 + b3)/255)*100
  speed - raw byte (not km/h)
  tps   - raw byte (not percent)
Do not call use_imperial()/use_metric(): imperial divides the raw Kawasaki
speed by 1.609344.

Run
---
Copy kwp2000.py to :lib/ and this file to the board, then run it or save it
as :main.py. Ctrl-C runs the cleanup block (disconnect + AP off).
"""

import time

import network
import socket

from machine import UART

from kwp2000 import KWP2000

try:
    import json
except ImportError:          # some builds only expose ujson
    import ujson as json


# ---------------------------------------------------------------------------
# Configuration - edit for your board / bike
# ---------------------------------------------------------------------------

BRAND = "kawasaki"
SSID = "Kawasaki-KWP2000"
PASSWORD = "kwp2000dash"      # WPA2 requires at least 8 characters

HTTP_PORT = 80

# Board profiles:
#   ESP32-C6 (default):   UART1, TX=GPIO10, RX=GPIO11
#   ESP32 classic/WROOM:  UART2, TX=GPIO17, RX=GPIO16
#   ESP32-WROVER/PICO:    UART2, TX=GPIO25, RX=GPIO26 (GPIO16/17 are PSRAM)
# On the C6, UART0 (GPIO16/17) is the REPL in the generic firmware and there
# is no normal UART2, so the classic UART2/17/16 profile raises ESP_FAIL.
# C6 alternatives if GPIO10/11 are not exposed on your board: GPIO18/19 or
# GPIO22/23. Avoid 4/5/8/9/15 (strapping), 12/13 (USB), 16/17 (UART0 REPL),
# and 24-30 (SPI flash).
UART_ID = 1
TX_PIN = 10                   # UART TX and fast-init GPIO
RX_PIN = 11                   # UART RX

POLL_INTERVAL_MS = 2000       # ECU sensor poll period
PAGE_REFRESH_MS = 1000        # browser fetch period
KEEPALIVE_INTERVAL_MS = 1000  # must stay <= 2000
LOOP_SLEEP_MS = 20
RECONNECT_MIN_MS = 2000
RECONNECT_MAX_MS = 60000
CLIENT_TIMEOUT_S = 0.5           # allow a slow phone to send all headers
MAX_REQUEST_BYTES = 2048         # cap the request-header read
LOG_LIMIT = 50                   # console lines kept for the browser
MAX_CLIENTS_PER_LOOP = 2
DEBUG_LEVEL = KWP2000.DEBUG_NONE   # DEBUG_DEFAULT during bring-up

# Blocking budget (no watchdog is enabled):
#   request_sensors() can block ~44-45 s on a silent Kawasaki bus
#   connect() up to ~7.5 s; disconnect() up to ~8.5 s
# If you enable machine.WDT, choose a timeout well above these and feed it
# between calls. It cannot be fed from inside a blocking library call.


# ---------------------------------------------------------------------------
# Cached state - the HTTP handler never touches the ECU directly
# ---------------------------------------------------------------------------

STATE = {
    "connected": False,
    "gear_raw": None,
    "rpm": None,              # int(round(...)) once decoded
    "speed_raw": None,
    "tps_raw": None,
    "poll_ok": False,
    "last_ok_ms": None,
    "errors": [],
}


# ---------------------------------------------------------------------------
# Console log (mirrored to the browser)
# ---------------------------------------------------------------------------

LOG_LINES = []


def _uptime_str():
    seconds = (time.ticks_ms() // 1000) % 86400
    return "%02d:%02d:%02d" % (
        seconds // 3600, (seconds // 60) % 60, seconds % 60
    )


def log(message):
    """Print to serial and append a timestamped copy for the browser."""
    message = str(message)
    print(message)
    LOG_LINES.append("%s %s" % (_uptime_str(), message))
    if len(LOG_LINES) > LOG_LIMIT:
        del LOG_LINES[0]


def _hex(data):
    if not data:
        return "<none>"
    return " ".join(["%02X" % b for b in data])


class BrowserLogStream:
    """Forward KWP2000 debug output to the serial console and browser."""

    def write(self, text):
        text = str(text)
        for line in text.splitlines():
            if line:
                log(line)
        return len(text)


# ---------------------------------------------------------------------------
# Web page (no external assets, so it works on the internet-less AP)
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = b"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kawasaki KWP2000</title>
<style>
  body { margin: 0; padding: 2rem 1rem; background: #111; color: #eee;
         font-family: system-ui, -apple-system, sans-serif; }
  main { max-width: 34rem; margin: 0 auto; }
  h1 { font-size: 1.35rem; margin: 0 0 1.25rem; font-weight: 600; }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: .75rem .9rem; border-bottom: 1px solid #2c2c2c; }
  th { text-align: left; color: #8ecae6; font-weight: 600; }
  td { text-align: right; font-size: 1.6rem;
       font-variant-numeric: tabular-nums; }
  #status { color: #aaa; font-size: .9rem; margin-top: 1rem; }
  .note { color: #777; font-size: .8rem; line-height: 1.4; }
  h2 { font-size: 1rem; margin: 1.5rem 0 .5rem; color: #8ecae6; }
  #log { margin: 0; padding: .75rem; height: 12rem; overflow-y: auto;
         background: #0a0a0a; border: 1px solid #2c2c2c;
         border-radius: .4rem; color: #bbb;
         font: .75rem/1.4 ui-monospace, Menlo, Consolas, monospace;
         white-space: pre-wrap; word-break: break-word; }
</style>
</head>
<body>
<main>
<h1>Kawasaki KWP2000</h1>
<table>
  <tr><th>Gear (GPS raw)</th><td id="gear">n/a</td></tr>
  <tr><th>RPM</th><td id="rpm">n/a</td></tr>
  <tr><th>Speed (raw)</th><td id="speed">n/a</td></tr>
  <tr><th>TPS (raw)</th><td id="tps">n/a</td></tr>
</table>
<p id="status">connecting...</p>
<p class="note">Speed and TPS are raw ECU bytes, not km/h or percent.
Gear is the raw GPS (Gear Position Sensor) byte. RPM is computed by the
library from the two RPM bytes.</p>
<h2>Console</h2>
<pre id="log" aria-live="polite"></pre>
</main>
<script>
function fmt(v) {
  return (v === null || v === undefined) ? "n/a" : String(v);
}
function refresh() {
  function done() { setTimeout(refresh, __REFRESH_MS__); }
  fetch("/data?t=" + Date.now(), {cache: "no-store"})
    .then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    })
    .then(function (d) {
      document.getElementById("gear").textContent = fmt(d.gear_raw);
      document.getElementById("rpm").textContent = fmt(d.rpm);
      document.getElementById("speed").textContent = fmt(d.speed_raw);
      document.getElementById("tps").textContent = fmt(d.tps_raw);
      var text = d.connected ? (d.poll_ok ? "live" : "connected, stale")
                             : "reconnecting";
      if (d.age_s !== null && d.age_s !== undefined) {
        text += " / last full update " + d.age_s + " s ago";
      }
      if (d.errors && d.errors.length) {
        text += " [" + d.errors.join(", ") + "]";
      }
      document.getElementById("status").textContent = text;

      var logEl = document.getElementById("log");
      var atBottom = logEl.scrollTop + logEl.clientHeight >=
                     logEl.scrollHeight - 8;
      logEl.textContent = (d.log || []).join(String.fromCharCode(10));
      if (atBottom) {
        logEl.scrollTop = logEl.scrollHeight;
      }
    })
    .catch(function () {
      document.getElementById("status").textContent = "ESP32 unreachable";
    })
    .then(done, done);
}
refresh();
</script>
</body>
</html>
"""

INDEX_HTML = _HTML_TEMPLATE.replace(
    b"__REFRESH_MS__", str(PAGE_REFRESH_MS).encode()
)


# ---------------------------------------------------------------------------
# WiFi access point and HTTP server
# ---------------------------------------------------------------------------

def start_ap():
    """Start the access point and print the address to open on the phone."""
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=SSID, password=PASSWORD)
    ip = ap.ifconfig()[0]
    log("WiFi AP: %s" % SSID)
    log("Open in a browser: http://%s/  (not localhost)" % ip)
    return ap


def open_server():
    """Create the non-blocking HTTP listener."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass
    server.bind(("0.0.0.0", HTTP_PORT))
    server.listen(4)
    server.setblocking(False)
    return server


def send_response(client, status, content_type, body):
    """Send a small HTTP response with explicit length and no caching."""
    if not isinstance(body, (bytes, bytearray)):
        body = body.encode()
    headers = (
        "HTTP/1.1 %s\r\n"
        "Content-Type: %s\r\n"
        "Content-Length: %d\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        "\r\n" % (status, content_type, len(body))
    ).encode()
    client.sendall(headers + body)


def serve_pending(server):
    """Accept and answer a bounded number of waiting HTTP clients."""
    for _ in range(MAX_CLIENTS_PER_LOOP):
        try:
            client, _addr = server.accept()
        except OSError:
            return          # EAGAIN on a non-blocking socket is normal
        if client is None:  # defensive: some ports return (None, None)
            return

        try:
            client.settimeout(CLIENT_TIMEOUT_S)
            # Read the complete request header block. Closing a socket with
            # unread data makes lwIP send RST instead of FIN, which browsers
            # report as a reset connection.
            request = b""
            while (b"\r\n\r\n" not in request and
                   len(request) < MAX_REQUEST_BYTES):
                chunk = client.recv(256)
                if not chunk:
                    break
                request += chunk
            if b"\r\n\r\n" not in request:
                continue

            line = request.split(b"\r\n", 1)[0]
            parts = line.split()
            if len(parts) < 2:
                continue

            method = parts[0]
            path = parts[1].split(b"?", 1)[0]

            if method != b"GET":
                send_response(client, "405 Method Not Allowed",
                              "text/plain; charset=utf-8", b"GET only\n")
            elif path in (b"/", b"/index.html"):
                send_response(client, "200 OK",
                              "text/html; charset=utf-8", INDEX_HTML)
            elif path == b"/data":
                send_response(client, "200 OK",
                              "application/json; charset=utf-8",
                              build_status_json())
            else:
                send_response(client, "404 Not Found",
                              "text/plain; charset=utf-8", b"Not found\n")
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Sensor polling and JSON snapshot
# ---------------------------------------------------------------------------

def build_status_json():
    """Return the cached snapshot as JSON bytes for /data."""
    age_s = None
    if STATE["last_ok_ms"] is not None:
        age_s = time.ticks_diff(
            time.ticks_ms(), STATE["last_ok_ms"]
        ) // 1000

    return json.dumps({
        "connected": STATE["connected"],
        "gear_raw": STATE["gear_raw"],
        "rpm": STATE["rpm"],
        "speed_raw": STATE["speed_raw"],
        "tps_raw": STATE["tps_raw"],
        "poll_ok": STATE["poll_ok"],
        "age_s": age_s,
        "errors": STATE["errors"],
        "log": LOG_LINES,
    }).encode()


def poll_sensors(ecu):
    """Refresh the cached snapshot from the ECU.

    ``request_sensors()`` makes seven sequential Kawasaki requests. Sensor
    values that could not be decoded keep their previous value; we only mark
    ``poll_ok`` when the call succeeded without a new ERR_HEADER bit.
    """
    before = ecu.error_flags
    ok = ecu.request_sensors()
    new_errors = ecu.error_flags & ~before

    STATE["gear_raw"] = ecu.gps
    STATE["rpm"] = None if ecu.rpm is None else int(round(ecu.rpm))
    STATE["speed_raw"] = ecu.speed
    STATE["tps_raw"] = ecu.tps
    values_ok = all(
        STATE[key] is not None
        for key in ("gear_raw", "rpm", "speed_raw", "tps_raw")
    )
    STATE["connected"] = ecu.connected
    STATE["errors"] = list(ecu.errors)
    STATE["poll_ok"] = bool(
        ok and values_ok and not (new_errors & KWP2000.ERR_HEADER)
    )

    if STATE["poll_ok"]:
        STATE["last_ok_ms"] = time.ticks_ms()
    else:
        log("sensor poll incomplete: code=%s nrc=%s new=0x%04X errors=%s" %
            (ecu.last_response_code, ecu.last_nrc, new_errors, ecu.errors))


# ---------------------------------------------------------------------------
# UART setup and main loop
# ---------------------------------------------------------------------------

def create_uart():
    """Create the K-line UART, with board-aware guidance on failure."""
    try:
        return UART(UART_ID, baudrate=10400, tx=TX_PIN, rx=RX_PIN)
    except OSError as exc:
        log("UART%d init failed on TX=%s RX=%s: %s" %
            (UART_ID, TX_PIN, RX_PIN, exc))
        log("The pins or UART id are reserved on this board.")
        log("ESP32-C6: use UART_ID=1, TX_PIN=10, RX_PIN=11.")
        log("  UART0/GPIO16-17 is the REPL; there is no normal UART2.")
        log("ESP32 classic/WROOM: UART_ID=2, TX_PIN=17, RX_PIN=16.")
        log("ESP32-WROVER/PICO: avoid GPIO16/17 (PSRAM); try 25/26.")
        log("Rewire the interface to the configured pins, then retry.")
        raise


def main():
    uart = create_uart()
    ecu = KWP2000(uart, BRAND, tx_pin=TX_PIN, rx_pin=RX_PIN)
    if DEBUG_LEVEL:
        ecu.enable_debug(stream=BrowserLogStream(), level=DEBUG_LEVEL)

    ap = start_ap()
    server = open_server()

    next_connect = time.ticks_ms()
    next_poll = time.ticks_ms()
    backoff = RECONNECT_MIN_MS
    last_connect_errors = None
    kline_hint_shown = False

    try:
        while True:
            serve_pending(server)
            now = time.ticks_ms()

            if not ecu.connected:
                STATE["connected"] = False
                STATE["poll_ok"] = False
                if time.ticks_diff(now, next_connect) >= 0:
                    log("Connecting to ECU...")
                    if ecu.connect():
                        log("ECU connected")
                        STATE["connected"] = True
                        STATE["errors"] = list(ecu.errors)
                        backoff = RECONNECT_MIN_MS
                        last_connect_errors = None
                        next_poll = time.ticks_ms()
                    else:
                        log("connect failed: code=%s nrc=%s errors=%s" %
                            (ecu.last_response_code, ecu.last_nrc,
                             ecu.errors))
                        if ecu.errors != last_connect_errors:
                            log("  frame options: %s" %
                                (ecu.frame_options,))
                            log("  last response: %s" %
                                _hex(ecu.last_response))
                            log("  last payload: %s" %
                                _hex(ecu.last_response_payload))
                            last_connect_errors = ecu.errors
                        if not kline_hint_shown:
                            log("K-line tip: module TX->GPIO10, "
                                "module RX<-GPIO11, common ground;")
                            log("  check the bike's K-line pin and set "
                                "DEBUG_LEVEL=DEBUG_VERBOSE.")
                            kline_hint_shown = True
                        STATE["errors"] = list(ecu.errors)
                        next_connect = time.ticks_add(
                            time.ticks_ms(), backoff
                        )
                        backoff = min(backoff * 2, RECONNECT_MAX_MS)
                time.sleep_ms(LOOP_SLEEP_MS)
                continue

            if not ecu.keep_alive(KEEPALIVE_INTERVAL_MS):
                log("link lost: %s" % (ecu.errors,))
                STATE["connected"] = False
                STATE["poll_ok"] = False
                STATE["errors"] = list(ecu.errors)
                next_connect = time.ticks_add(time.ticks_ms(), backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_MS)
                continue

            if time.ticks_diff(time.ticks_ms(), next_poll) >= 0:
                started = time.ticks_ms()
                poll_sensors(ecu)
                next_poll = time.ticks_add(started, POLL_INTERVAL_MS)
                if time.ticks_diff(time.ticks_ms(), next_poll) >= 0:
                    # The poll overran its slot; wait a full interval.
                    next_poll = time.ticks_add(
                        time.ticks_ms(), POLL_INTERVAL_MS
                    )

                # A long failed poll may have passed the 2 s link expiry.
                if not ecu.keep_alive(KEEPALIVE_INTERVAL_MS):
                    log("link lost after poll: %s" % (ecu.errors,))
                    STATE["connected"] = False
                    STATE["poll_ok"] = False
                    STATE["errors"] = list(ecu.errors)
                    next_connect = time.ticks_add(
                        time.ticks_ms(), backoff
                    )
                    backoff = min(backoff * 2, RECONNECT_MAX_MS)

            time.sleep_ms(LOOP_SLEEP_MS)
    finally:
        try:
            server.close()
        except OSError:
            pass
        log("Disconnecting ECU (up to ~8.5 s)...")
        ecu.disconnect()
        try:
            ap.active(False)
        except OSError:
            pass
        log("Stopped")


if __name__ == "__main__":
    main()
