"""Test doubles for the host-side KWP2000 tests.

These are deliberately dependency-free and work with plain CPython. The fake
UART echoes every transmitted byte (like a K-line transceiver) and can hand the
byte stream to a FakeECU that answers complete request frames.
"""

FORMAT_PHYSICAL = 0x80
OUR_ADDR = 0xF1
START_COM = b"\x81"
STOP_COM = b"\x82"
START_DIAGNOSTIC = b"\x10\x80"


class FakeClock:
    """Monotonic millisecond clock that advances instantly on sleep_ms()."""

    def __init__(self):
        self.now = 0
        self.sleeps = []

    def ticks_ms(self):
        return self.now

    def ticks_diff(self, a, b):
        return a - b

    def sleep_ms(self, ms):
        self.sleeps.append(ms)
        self.now += ms


class FakePin:
    """Minimal machine.Pin-like output with timestamped transitions."""

    def __init__(self, clock=None, value=1):
        self.clock = clock
        self._value = int(bool(value))
        self.transitions = []

    def _set(self, value):
        self._value = int(bool(value))
        self.transitions.append((self.clock.now if self.clock else None, self._value))

    def init(self, mode=None, value=None):
        if value is not None:
            self._set(value)

    def value(self, value=None):
        if value is None:
            return self._value
        self._set(value)


class FakeUART:
    """UART double: records TX, optionally echoes each byte into RX."""

    def __init__(self, clock, echo=True):
        self.clock = clock
        self.echo = echo
        self.rx = bytearray()
        self.tx = bytearray()
        self.init_calls = []
        self.on_write = None

    def init(self, **kwargs):
        self.init_calls.append(kwargs)

    def write(self, data):
        if isinstance(data, int):
            data = bytes((data,))
        for byte in data:
            self.tx.append(byte)
            if self.echo:
                self.rx.append(byte)
            if self.on_write is not None:
                self.on_write(byte)
        return len(data)

    def any(self):
        return len(self.rx)

    def read(self, n=None):
        if not self.rx:
            return None
        if n is None or n < 0 or n > len(self.rx):
            n = len(self.rx)
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def flush(self):
        pass


class FakeECU:
    """Byte-level KWP2000 ECU simulator.

    Requests are parsed from the byte stream (the format byte decides whether a
    length byte is present; both address layouts are attempted). A complete,
    checksum-valid request is recorded and answered by appending a response
    frame to the UART's RX buffer.
    """

    def __init__(self, uart, ecu_addr=0x12, our_addr=OUR_ADDR,
                 response_addresses=True, response_length_byte=True):
        self.uart = uart
        self.ecu_addr = ecu_addr
        self.our_addr = our_addr
        self.response_addresses = response_addresses
        self.response_length_byte = response_length_byte

        self.buffer = bytearray()
        self.requests = []          # request payloads, in order
        self.responses = {}         # request payload -> response payload (or callable)
        self.sensor_payloads = {}   # request payload -> response payload
        self.start_payload = b"\xC1\xEA\x8F"
        self.dtc_payload = None
        self.silent = False
        self.checksum_delta = 0

    # -- request parsing --------------------------------------------------

    def on_write(self, byte):
        self.buffer.append(byte)
        parsed = self._extract_request()
        if parsed is None:
            return

        frame, header_len = parsed
        payload = bytes(frame[header_len:-1])
        self.requests.append(payload)

        if self.silent:
            return

        response = self.response_for(payload)
        if response is None:
            return

        frame_out = make_response_frame(
            response,
            ecu_addr=self.ecu_addr,
            our_addr=self.our_addr,
            addresses=self.response_addresses,
            length_byte=self.response_length_byte,
            checksum_delta=self.checksum_delta,
        )
        self.uart.rx.extend(frame_out)

    def _extract_request(self):
        if not self.buffer:
            return None
        data = bytes(self.buffer)
        for addresses in (True, False):
            result = self._try_request(data, addresses)
            if result is not None:
                frame, header_len = result
                del self.buffer[:len(frame)]
                return frame, header_len
        return None

    def _try_request(self, data, addresses):
        b0 = data[0]
        embedded = (b0 & 0x3F) != 0
        header_len = 1 + (2 if addresses else 0) + (0 if embedded else 1)
        if len(data) < header_len:
            return None

        data_len = (b0 & 0x3F) if embedded else data[header_len - 1]
        total = header_len + data_len + 1
        if len(data) < total:
            return None

        frame = data[:total]
        if (sum(frame[:-1]) & 0xFF) != frame[-1]:
            return None
        if addresses:
            if frame[1] != self.ecu_addr or frame[2] != self.our_addr:
                return None
        return frame, header_len

    # -- responses --------------------------------------------------------

    def response_for(self, payload):
        if payload in self.responses:
            response = self.responses[payload]
            return response(self, payload) if callable(response) else response

        if payload == START_COM:
            return self.start_payload
        if payload == START_DIAGNOSTIC:
            return b"\x50\x80"
        if payload[:1] == b"\x21":
            return self.sensor_payloads.get(payload)
        if payload[:1] == b"\x3E":
            return b"\x7E" + payload[1:2]
        if payload == STOP_COM:
            return b"\xC2"
        if payload[:1] in (b"\x13", b"\x17", b"\x18"):
            if self.dtc_payload is not None:
                return self.dtc_payload
            return bytes((payload[0] | 0x40, 0x00))
        if payload[:1] == b"\x14":
            return b"\x54"
        return bytes((payload[0] | 0x40,))


def make_response_frame(payload, ecu_addr=0x12, our_addr=OUR_ADDR,
                        addresses=True, length_byte=True, checksum_delta=0):
    """Build an ECU response frame (target = tester, source = ECU)."""
    n = len(payload)
    embedded = (not length_byte) and n <= 0x3F

    frame = bytearray()
    frame.append((FORMAT_PHYSICAL | n) if embedded else FORMAT_PHYSICAL)
    if addresses:
        frame.append(our_addr)
        frame.append(ecu_addr)
    if not embedded:
        frame.append(n)
    frame.extend(payload)
    checksum = (sum(frame) & 0xFF)
    frame.append((checksum + checksum_delta) & 0xFF)
    return bytes(frame)


def make_request_frame(payload, ecu_addr=0x12, our_addr=OUR_ADDR,
                       addresses=True, length_byte=True):
    """Build a tester request frame (target = ECU, source = tester)."""
    n = len(payload)
    embedded = (not length_byte) and n <= 0x3F

    frame = bytearray()
    frame.append((FORMAT_PHYSICAL | n) if embedded else FORMAT_PHYSICAL)
    if addresses:
        frame.append(ecu_addr)
        frame.append(our_addr)
    if not embedded:
        frame.append(n)
    frame.extend(payload)
    frame.append(sum(frame) & 0xFF)
    return bytes(frame)


def suzuki_sensor_payload(gps=3, clutch=1, rpm_h=50, rpm_l=120, speed=100,
                          tps=100, stps=51, iap=100, iat=80, ect=112,
                          voltage=16):
    """Build a 52-byte Suzuki sensor response payload with known values.

    The first two bytes are the positive service and local identifier; sensor
    bytes are placed at the offsets the driver expects (relative to the end of
    that two-byte prefix).
    """
    payload = bytearray(52)
    payload[0] = 0x61
    payload[1] = 0x08
    payload[2 + 20] = gps
    payload[2 + 46] = clutch
    payload[2 + 11] = rpm_h
    payload[2 + 12] = rpm_l
    payload[2 + 10] = speed
    payload[2 + 13] = tps
    payload[2 + 40] = stps
    payload[2 + 14] = iap
    payload[2 + 16] = iat
    payload[2 + 15] = ect
    payload[2 + 18] = voltage
    return bytes(payload)
