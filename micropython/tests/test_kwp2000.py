"""Host-side tests for micropython/kwp2000.py.

Run from the repository root:

    python -m unittest discover -s micropython/tests -p "test_*.py" -v
"""

import os
import sys
import unittest
from io import StringIO
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

import kwp2000
from fakes import (
    FakeClock,
    FakeECU,
    FakePin,
    FakeUART,
    make_response_frame,
    suzuki_sensor_payload,
)


class KWPTestCase(unittest.TestCase):
    brand = "suzuki"
    ecu_addr = 0x12
    response_addresses = True
    response_length_byte = True

    def setUp(self):
        self.clock = FakeClock()
        self.pin = FakePin(self.clock)
        self.uart = FakeUART(self.clock)
        self.ecu = FakeECU(
            self.uart,
            ecu_addr=self.ecu_addr,
            response_addresses=self.response_addresses,
            response_length_byte=self.response_length_byte,
        )
        self.uart.on_write = self.ecu.on_write

        self._patchers = [
            mock.patch.object(kwp2000, "ticks_ms", self.clock.ticks_ms),
            mock.patch.object(kwp2000, "ticks_diff", self.clock.ticks_diff),
            mock.patch.object(kwp2000, "sleep_ms", self.clock.sleep_ms),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.device = kwp2000.KWP2000(
            self.uart, self.brand, tx_pin=self.pin, rx_pin=16
        )

    def connect_suzuki(self):
        self.ecu.sensor_payloads[b"\x21\x08"] = suzuki_sensor_payload()
        self.assertTrue(self.device.connect())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestFrameBuilder(unittest.TestCase):
    def test_short_format_no_addresses(self):
        frame = kwp2000._build_request(b"\x21\x08", 0x12, False, False)
        self.assertEqual(frame, b"\x82\x21\x08\xAB")

    def test_short_format_with_addresses(self):
        frame = kwp2000._build_request(b"\x81", 0x12, False, True)
        self.assertEqual(frame, b"\x81\x12\xF1\x81\x05")

    def test_length_byte_no_addresses(self):
        frame = kwp2000._build_request(b"\x21\x08", 0x12, True, False)
        self.assertEqual(frame, b"\x80\x02\x21\x08\xAB")

    def test_length_byte_with_addresses(self):
        frame = kwp2000._build_request(b"\x21\x08", 0x12, True, True)
        self.assertEqual(frame, b"\x80\x12\xF1\x02\x21\x08\xAE")

    def test_payload_of_64_forces_length_byte(self):
        payload = bytes(range(64))
        frame = kwp2000._build_request(payload, 0x12, False, True)
        self.assertEqual(frame[0], 0x80)
        self.assertEqual(frame[3], 64)
        self.assertEqual(kwp2000._checksum(frame[:-1]), frame[-1])

    def test_empty_payload_rejected(self):
        with self.assertRaises(ValueError):
            kwp2000._build_request(b"", 0x12, False, True)

    def test_oversized_payload_rejected(self):
        with self.assertRaises(ValueError):
            kwp2000._build_request(b"\x00" * 256, 0x12, False, True)

    def test_checksum_known_vectors(self):
        self.assertEqual(kwp2000._checksum(b"\x81\x12\xF1\x81"), 0x05)
        self.assertEqual(kwp2000._checksum(b"\x80\x12\xF1\x02\x21\x08"), 0xAE)

    def test_auto_modes_are_false_for_requests(self):
        frame = kwp2000._build_request(b"\x21\x08", 0x12, None, None)
        self.assertEqual(frame, b"\x82\x21\x08\xAB")


class TestKeyBytes(unittest.TestCase):
    def test_ea_8f_requires_length_and_addresses(self):
        self.assertEqual(
            kwp2000._parse_key_bytes(0xEA, 0x8F),
            (True, True, "normal", 0),
        )

    def test_e5_8f_no_length_no_addresses(self):
        self.assertEqual(
            kwp2000._parse_key_bytes(0xE5, 0x8F),
            (False, False, "normal", 0),
        )

    def test_d0_8f_auto_mode(self):
        use_lb, use_addr, timing, errors = kwp2000._parse_key_bytes(0xD0, 0x8F)
        self.assertEqual(use_lb, "auto")
        self.assertEqual(use_addr, "auto")
        self.assertEqual(timing, "auto")
        # The auto pattern shares the extended-timing bits, matching the C++.
        self.assertTrue(errors & kwp2000.ERR_UNSUPPORTED)

    def test_d5_8f_extended_timing(self):
        use_lb, use_addr, timing, errors = kwp2000._parse_key_bytes(0xD5, 0x8F)
        self.assertFalse(use_lb)
        self.assertFalse(use_addr)
        self.assertEqual(timing, "extended")
        self.assertTrue(errors & kwp2000.ERR_UNSUPPORTED)

    def test_bad_high_byte_sets_config(self):
        self.assertTrue(
            kwp2000._parse_key_bytes(0xEA, 0x8E)[3] & kwp2000.ERR_CONFIG
        )

    def test_bad_parity_sets_config(self):
        self.assertTrue(
            kwp2000._parse_key_bytes(0x6A, 0x8F)[3] & kwp2000.ERR_CONFIG
        )

    def test_bit6_must_be_one(self):
        self.assertTrue(
            kwp2000._parse_key_bytes(0x2A, 0x8F)[3] & kwp2000.ERR_CONFIG
        )


class TestResponseParser(unittest.TestCase):
    def test_short_format_no_addresses(self):
        frame = make_response_frame(
            b"\x61\x08", addresses=False, length_byte=False
        )
        header = kwp2000._response_geometry(frame, False)
        self.assertEqual(header[:3], (1, 2, 4))
        payload, ok, errors = kwp2000._parse_response(
            frame, header[0], False, 0x12
        )
        self.assertEqual(payload, b"\x61\x08")
        self.assertTrue(ok)
        self.assertEqual(errors, 0)

    def test_short_format_with_addresses(self):
        frame = make_response_frame(
            b"\x61\x08", addresses=True, length_byte=False
        )
        header = kwp2000._response_geometry(frame, True)
        self.assertEqual(header[:3], (3, 2, 6))

    def test_length_byte_no_addresses(self):
        frame = make_response_frame(
            b"\x61\x08", addresses=False, length_byte=True
        )
        header = kwp2000._response_geometry(frame, False)
        self.assertEqual(header[:3], (2, 2, 5))

    def test_length_byte_with_addresses(self):
        frame = make_response_frame(
            b"\x61\x08", addresses=True, length_byte=True
        )
        header = kwp2000._response_geometry(frame, True)
        self.assertEqual(header[:3], (4, 2, 7))

    def test_bad_checksum_sets_flag(self):
        frame = make_response_frame(
            b"\x61", addresses=True, length_byte=True, checksum_delta=1
        )
        header = kwp2000._response_geometry(frame, True)
        _payload, ok, errors = kwp2000._parse_response(
            frame, header[0], True, 0x12
        )
        self.assertFalse(ok)
        self.assertTrue(errors & kwp2000.ERR_CHECKSUM)

    def test_auto_detects_embedded_length(self):
        frame = make_response_frame(
            b"\x61", addresses=False, length_byte=False
        )
        header = kwp2000._response_geometry(frame, None)
        self.assertEqual(header[0], 1)
        self.assertFalse(header[3])

    def test_auto_detects_length_byte(self):
        frame = make_response_frame(
            b"\x61", addresses=False, length_byte=True
        )
        header = kwp2000._response_geometry(frame, None)
        self.assertEqual(header[0], 2)
        self.assertFalse(header[3])

    def test_auto_detects_addresses_from_our_addr(self):
        frame = make_response_frame(
            b"\x61", addresses=True, length_byte=True
        )
        header = kwp2000._response_geometry(frame, None)
        self.assertTrue(header[3])
        self.assertEqual(header[0], 4)

    def test_non_physical_format_sets_header_error(self):
        frame = make_response_frame(
            b"\x61", addresses=False, length_byte=False
        )
        frame = bytes((0xC1,)) + frame[1:]
        header = kwp2000._response_geometry(frame, False)
        _payload, _ok, errors = kwp2000._parse_response(
            frame, header[0], False, 0x12
        )
        self.assertTrue(errors & kwp2000.ERR_HEADER)

    def test_wrong_target_sets_to_error(self):
        frame = make_response_frame(
            b"\x61", addresses=True, length_byte=True
        )
        frame = bytes((frame[0], 0xF2)) + frame[2:]
        header = kwp2000._response_geometry(frame, True)
        _payload, _ok, errors = kwp2000._parse_response(
            frame, header[0], True, 0x12
        )
        self.assertTrue(errors & kwp2000.ERR_TO)

    def test_wrong_source_sets_from_error(self):
        frame = make_response_frame(
            b"\x61", addresses=True, length_byte=True
        )
        frame = bytes((frame[0], frame[1], 0x13)) + frame[3:]
        header = kwp2000._response_geometry(frame, True)
        _payload, _ok, errors = kwp2000._parse_response(
            frame, header[0], True, 0x12
        )
        self.assertTrue(errors & kwp2000.ERR_FROM)

    def test_truncated_frame_waits_for_more_bytes(self):
        frame = make_response_frame(
            b"\x61\x08", addresses=True, length_byte=True
        )
        self.assertIsNone(kwp2000._response_geometry(frame[:3], True))
        header = kwp2000._response_geometry(frame[:5], True)
        self.assertIsNotNone(header)
        self.assertGreater(header[2], 5)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

class TestConnectSuzuki(KWPTestCase):
    def test_connect_waveform_and_first_frame(self):
        self.connect_suzuki()
        self.assertEqual(
            self.pin.transitions,
            [(0, 1), (1000, 0), (1025, 1)],
        )
        self.assertTrue(
            self.uart.tx.startswith(b"\x81\x12\xF1\x81\x05")
        )

    def test_connect_already_connected_no_io(self):
        self.connect_suzuki()
        transitions = list(self.pin.transitions)
        tx_len = len(self.uart.tx)
        self.assertTrue(self.device.connect())
        self.assertEqual(self.pin.transitions, transitions)
        self.assertEqual(len(self.uart.tx), tx_len)

    def test_connect_failure_sets_start_error(self):
        self.ecu.silent = True
        self.assertFalse(self.device.connect())
        self.assertTrue(self.device.error_flags & kwp2000.ERR_START)
        self.assertFalse(self.device.connected)

    def test_connect_start_request_sent_once(self):
        self.connect_suzuki()
        self.assertEqual(self.ecu.requests.count(b"\x81"), 1)

    def test_connect_configures_frame_from_key_bytes(self):
        self.connect_suzuki()
        options = self.device.frame_options
        self.assertTrue(options["use_length_byte"])
        self.assertTrue(options["use_addresses"])
        self.assertEqual(options["timing"], "normal")

    def test_connect_reinitializes_uart_at_10400(self):
        self.connect_suzuki()
        self.assertTrue(self.uart.init_calls)
        last = self.uart.init_calls[-1]
        self.assertEqual(last["baudrate"], 10400)
        self.assertIs(last["tx"], self.pin)
        self.assertEqual(last["rx"], 16)

    def test_connect_auto_key_bytes_resolve_on_next_response(self):
        self.ecu.start_payload = b"\xC1\xD0\x8F"
        self.ecu.sensor_payloads[b"\x21\x08"] = suzuki_sensor_payload()
        self.assertTrue(self.device.connect())
        self.assertEqual(self.device.frame_options["use_length_byte"], None)
        self.assertEqual(self.device.frame_options["use_addresses"], None)

        self.ecu.responses[b"\x3E\x01"] = b"\x7E\x01"
        self.assertTrue(self.device.handle_request(b"\x3E\x01", attempts=1))
        self.assertTrue(self.device.frame_options["use_length_byte"])
        self.assertTrue(self.device.frame_options["use_addresses"])


class TestConnectKawasaki(KWPTestCase):
    brand = "kawasaki"
    ecu_addr = 0x11

    def test_kawasaki_sends_diagnostic_session(self):
        self.assertTrue(self.device.connect())
        self.assertEqual(self.ecu.requests[:2], [b"\x81", b"\x10\x80"])
        self.assertTrue(self.device.connected)
        # After key-byte negotiation the request must use a separate length
        # byte and both address bytes.
        self.assertIn(b"\x80\x11\xF1\x02\x10\x80\x14", self.uart.tx)

    def test_kawasaki_start_frame_uses_ecu_address_11(self):
        self.assertTrue(self.device.connect())
        self.assertTrue(
            self.uart.tx.startswith(b"\x81\x11\xF1\x81\x04")
        )


class TestYamaha(KWPTestCase):
    brand = "yamaha"
    ecu_addr = 0x3F
    response_addresses = False

    def test_connect_and_sensors_unsupported(self):
        self.assertTrue(self.device.connect())
        self.assertFalse(self.device.request_sensors())
        self.assertTrue(self.device.error_flags & kwp2000.ERR_UNSUPPORTED)


# ---------------------------------------------------------------------------
# Raw requests and response handling
# ---------------------------------------------------------------------------

class TestHandleRequest(KWPTestCase):
    def setUp(self):
        super().setUp()
        self.connect_suzuki()

    def test_positive_response_returns_true(self):
        self.ecu.responses[b"\x21\x08"] = b"\x61\x08"
        self.assertTrue(self.device.handle_request(b"\x21\x08", attempts=1))
        self.assertEqual(self.device.last_response_payload, b"\x61\x08")
        self.assertEqual(self.device.last_response_code, 0)

    def test_last_response_is_exposed(self):
        self.ecu.responses[b"\x3E\x01"] = b"\x7E\x01"
        self.assertTrue(self.device.handle_request(b"\x3E\x01", attempts=1))
        self.assertEqual(self.device.last_response_payload, b"\x7E\x01")
        self.assertEqual(self.device.last_response_service, 0x7E)
        self.assertIn(b"\x7E\x01", self.device.last_response)

    def test_negative_response_nrc_mapping(self):
        cases = [
            (0x10, -2),
            (0x11, -3),
            (0x12, -4),
            (0x21, -5),
            (0x22, -6),
            (0x78, -7),
            (0x55, -8),
        ]
        for nrc, expected in cases:
            self.ecu.responses[b"\x21\x08"] = bytes((0x7F, 0x21, nrc))
            self.assertFalse(
                self.device.handle_request(b"\x21\x08", attempts=1)
            )
            self.assertEqual(self.device.last_response_code, expected)
            self.assertEqual(self.device.last_nrc, nrc)
            self.assertTrue(self.device.error_flags & kwp2000.ERR_RESPONSE)

    def test_reject_for_other_service_sets_wrong_reject(self):
        self.ecu.responses[b"\x21\x08"] = b"\x7F\x3E\x11"
        self.assertFalse(self.device.handle_request(b"\x21\x08", attempts=1))
        self.assertTrue(self.device.error_flags & kwp2000.ERR_WRONG_REJECT)

    def test_bad_checksum_is_failure(self):
        self.ecu.checksum_delta = 1
        self.ecu.responses[b"\x21\x08"] = b"\x61\x08"
        self.assertFalse(self.device.handle_request(b"\x21\x08", attempts=1))
        self.assertTrue(self.device.error_flags & kwp2000.ERR_CHECKSUM)

    def test_unexpected_service_sets_flags(self):
        self.ecu.responses[b"\x21\x08"] = b"\x62\x08"
        self.assertFalse(self.device.handle_request(b"\x21\x08", attempts=1))
        self.assertTrue(self.device.error_flags & kwp2000.ERR_UNEXPECTED)
        self.assertEqual(self.device.last_response_code, -9)

    def test_no_response_returns_false(self):
        self.ecu.silent = True
        self.assertFalse(self.device.handle_request(b"\x21\x08", attempts=1))
        self.assertEqual(self.device.last_response_code, -1)

    def test_retries_three_times_by_default(self):
        self.ecu.silent = True
        before = self.ecu.requests.count(b"\x21\x08")
        self.assertFalse(self.device.handle_request(b"\x21\x08"))
        self.assertEqual(self.ecu.requests.count(b"\x21\x08"), before + 3)

    def test_attempts_one_sends_once(self):
        self.ecu.silent = True
        before = self.ecu.requests.count(b"\x21\x08")
        self.assertFalse(self.device.handle_request(b"\x21\x08", attempts=1))
        self.assertEqual(self.ecu.requests.count(b"\x21\x08"), before + 1)

    def test_check_response_false_accepts_any_valid_frame(self):
        self.ecu.responses[b"\x21\x08"] = b"\x62\x08"
        self.assertTrue(
            self.device.handle_request(
                b"\x21\x08", attempts=1, check_response=False
            )
        )

    def test_invalid_payload_raises(self):
        with self.assertRaises(ValueError):
            self.device.handle_request(b"")


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------

class TestSuzukiSensors(KWPTestCase):
    def setUp(self):
        super().setUp()
        self.connect_suzuki()

    def test_before_request_properties_are_none(self):
        self.assertIsNone(self.device.rpm)
        self.assertIsNone(self.device.speed)
        self.assertIsNone(self.device.ect)
        self.assertIsNone(self.device.voltage)

    def test_decode_all_values(self):
        self.assertTrue(self.device.request_sensors())
        self.assertEqual(self.device.gps, 3)
        self.assertEqual(self.device.clutch, 1)
        self.assertAlmostEqual(self.device.rpm, 512.0)
        self.assertEqual(self.device.speed, 200)
        self.assertAlmostEqual(self.device.tps, 125 * 45 / 201)
        self.assertAlmostEqual(self.device.stps, 20.0)
        self.assertAlmostEqual(self.device.iap, 54.4)
        self.assertAlmostEqual(self.device.iat, 20.0)
        self.assertAlmostEqual(self.device.ect, 40.0)
        self.assertAlmostEqual(self.device.voltage, 1600 / 126)

    def test_rpm_not_truncated_to_byte(self):
        self.assertTrue(self.device.request_sensors())
        self.assertGreater(self.device.rpm, 255)

    def test_offsets_relative_to_response_data(self):
        # Force a no-address, embedded-length exchange. The same sensor
        # payload must decode identically.
        self.device._use_addresses = False
        self.device._use_length_byte = False
        self.ecu.response_addresses = False
        self.ecu.response_length_byte = False
        self.assertTrue(self.device.handle_request(b"\x21\x08", attempts=1))
        self.assertTrue(self.device._decode_suzuki(self.device.last_response_payload))
        self.assertAlmostEqual(self.device.rpm, 512.0)
        self.assertEqual(self.device.speed, 200)

    def test_short_payload_keeps_previous_values(self):
        self.assertTrue(self.device.request_sensors())
        old_rpm = self.device.rpm
        self.ecu.sensor_payloads[b"\x21\x08"] = b"\x61\x08"
        self.assertFalse(self.device.request_sensors())
        self.assertEqual(self.device.rpm, old_rpm)
        self.assertTrue(self.device.error_flags & kwp2000.ERR_HEADER)

    def test_imperial_speed_does_not_mutate(self):
        self.assertTrue(self.device.request_sensors())
        metric = self.device.speed
        self.device.use_imperial()
        first = self.device.speed
        second = self.device.speed
        self.assertAlmostEqual(first, metric / 1.609344)
        self.assertAlmostEqual(second, first)
        self.device.use_metric()
        self.assertAlmostEqual(self.device.speed, metric)

    def test_imperial_temperature_does_not_mutate(self):
        self.assertTrue(self.device.request_sensors())
        metric = self.device.ect
        self.device.use_imperial()
        first = self.device.ect
        second = self.device.ect
        self.assertAlmostEqual(first, metric * 1.8 + 32.0)
        self.assertAlmostEqual(second, first)
        self.device.use_metric()
        self.assertAlmostEqual(self.device.ect, metric)

    def test_request_sensors_requires_connection(self):
        device = kwp2000.KWP2000(
            self.uart, "suzuki", tx_pin=self.pin, rx_pin=16
        )
        self.assertFalse(device.request_sensors())
        self.assertTrue(device.error_flags & kwp2000.ERR_USER)

    def test_sensors_property_applies_units(self):
        self.assertTrue(self.device.request_sensors())
        metric = self.device.sensors
        self.device.use_imperial()
        imperial = self.device.sensors
        self.assertAlmostEqual(
            imperial["speed"], metric["speed"] / 1.609344
        )
        self.assertAlmostEqual(
            imperial["ect"], metric["ect"] * 1.8 + 32.0
        )

    def test_print_sensors_output(self):
        stream = StringIO()
        self.device.enable_debug(stream, kwp2000.KWP2000.DEBUG_DEFAULT)
        self.assertTrue(self.device.request_sensors())
        self.device.print_sensors()
        output = stream.getvalue()
        self.assertIn("RPM: 512.0", output)
        self.assertIn("Speed: 200 km/h", output)
        self.assertIn("ECT: 40.0 C", output)


class TestKawasakiSensors(KWPTestCase):
    brand = "kawasaki"
    ecu_addr = 0x11

    def setUp(self):
        super().setUp()
        self.ecu.sensor_payloads.update({
            b"\x21\x0B": b"\x61\x0B\x04",
            b"\x21\x09": b"\x61\x09\x10\x20",
            b"\x21\x0C": b"\x61\x0C\x64",
            b"\x21\x04": b"\x61\x04\x33",
            b"\x21\x07": b"\x61\x07\x64",
            b"\x21\x05": b"\x61\x05\x50",
            b"\x21\x06": b"\x61\x06\x70",
        })
        self.assertTrue(self.device.connect())

    def test_request_sequence(self):
        self.assertTrue(self.device.request_sensors())
        sensor_requests = [
            request for request in self.ecu.requests
            if request[:1] == b"\x21"
        ]
        self.assertEqual(sensor_requests, [
            b"\x21\x0B",
            b"\x21\x09",
            b"\x21\x0C",
            b"\x21\x04",
            b"\x21\x07",
            b"\x21\x05",
            b"\x21\x06",
        ])

    def test_rpm_formula(self):
        self.assertTrue(self.device.request_sensors())
        expected = ((0x10 * 255 + 0x20) / 255.0) * 100
        self.assertAlmostEqual(self.device.rpm, expected)

    def test_raw_values(self):
        self.assertTrue(self.device.request_sensors())
        self.assertEqual(self.device.gps, 4)
        self.assertEqual(self.device.speed, 0x64)
        self.assertEqual(self.device.tps, 0x33)
        self.assertAlmostEqual(self.device.iap, 0x64 * 4 * 0.136)
        self.assertAlmostEqual(self.device.iat, (0x50 - 48) / 1.6)
        self.assertAlmostEqual(self.device.ect, (0x70 - 48) / 1.6)
        self.assertIsNone(self.device.clutch)
        self.assertIsNone(self.device.stps)
        self.assertIsNone(self.device.voltage)

    def test_partial_failure_keeps_previous_values(self):
        self.assertTrue(self.device.request_sensors())
        old_rpm = self.device.rpm
        self.ecu.silent = True
        self.assertFalse(self.device.request_sensors())
        self.assertEqual(self.device.rpm, old_rpm)


# ---------------------------------------------------------------------------
# Trouble codes
# ---------------------------------------------------------------------------

class TestTroubleCodes(KWPTestCase):
    def setUp(self):
        super().setUp()
        self.connect_suzuki()

    def test_read_active_returns_two_byte_records(self):
        self.ecu.dtc_payload = b"\x57\x02\x12\x34\x56\x78"
        self.assertEqual(
            self.device.read_trouble_codes(),
            [(0x1234, None), (0x5678, None)],
        )

    def test_read_all_returns_status(self):
        self.ecu.dtc_payload = b"\x58\x01\x12\x34\x05"
        self.assertEqual(
            self.device.read_trouble_codes(kwp2000.KWP2000.READ_ALL),
            [(0x1234, 0x05)],
        )

    def test_count_larger_than_data_sets_header_error(self):
        self.ecu.dtc_payload = b"\x57\x03\x12\x34"
        self.assertEqual(
            self.device.read_trouble_codes(),
            [(0x1234, None)],
        )
        self.assertTrue(self.device.error_flags & kwp2000.ERR_HEADER)

    def test_clear_all_sends_14(self):
        self.assertTrue(self.device.clear_trouble_codes())
        self.assertIn(b"\x14", self.ecu.requests)

    def test_clear_single_sends_14_and_code(self):
        self.assertTrue(self.device.clear_trouble_codes(0x12))
        self.assertIn(b"\x14\x12", self.ecu.requests)

    def test_not_connected_returns_empty(self):
        device = kwp2000.KWP2000(
            self.uart, "suzuki", tx_pin=self.pin, rx_pin=16
        )
        self.assertEqual(device.read_trouble_codes(), [])
        self.assertTrue(device.error_flags & kwp2000.ERR_USER)

    def test_read_total_uses_service_13(self):
        self.ecu.dtc_payload = b"\x53\x00"
        self.assertEqual(
            self.device.read_trouble_codes(kwp2000.KWP2000.READ_TOTAL), []
        )
        self.assertIn(b"\x13", self.ecu.requests)

    def test_invalid_selector_raises(self):
        with self.assertRaises(ValueError):
            self.device.read_trouble_codes(99)

    def test_invalid_clear_code_raises(self):
        with self.assertRaises(ValueError):
            self.device.clear_trouble_codes(300)


# ---------------------------------------------------------------------------
# Keep-alive and disconnect
# ---------------------------------------------------------------------------

class TestKeepAlive(KWPTestCase):
    def setUp(self):
        super().setUp()
        self.connect_suzuki()

    def test_not_connected_returns_false(self):
        device = kwp2000.KWP2000(
            self.uart, "suzuki", tx_pin=self.pin, rx_pin=16
        )
        self.assertFalse(device.keep_alive())

    def test_sends_tester_present_after_interval(self):
        before = self.ecu.requests.count(b"\x3E\x01")
        self.clock.now += 1600
        self.assertTrue(self.device.keep_alive())
        self.assertEqual(self.ecu.requests.count(b"\x3E\x01"), before + 1)

    def test_does_nothing_inside_interval(self):
        before = self.ecu.requests.count(b"\x3E\x01")
        self.clock.now += 100
        self.assertTrue(self.device.keep_alive())
        self.assertEqual(self.ecu.requests.count(b"\x3E\x01"), before)

    def test_expires_after_two_seconds(self):
        self.clock.now += 2100
        self.assertFalse(self.device.keep_alive())
        self.assertFalse(self.device.connected)
        self.assertTrue(self.device.error_flags & kwp2000.ERR_TIMEOUT)

    def test_interval_over_two_seconds_is_clamped(self):
        self.clock.now += 1600
        self.assertTrue(self.device.keep_alive(2500))
        self.assertTrue(self.device.error_flags & kwp2000.ERR_USER)
        self.assertEqual(self.ecu.requests.count(b"\x3E\x01"), 1)

    def test_drains_unsolicited_bytes(self):
        self.uart.rx.extend(b"\xAA\xBB")
        self.clock.now += 100
        self.assertTrue(self.device.keep_alive())
        self.assertEqual(len(self.uart.rx), 0)


class TestKeepAliveKawasaki(KWPTestCase):
    brand = "kawasaki"
    ecu_addr = 0x11

    def setUp(self):
        super().setUp()
        self.ecu.sensor_payloads[b"\x21\x0B"] = b"\x61\x0B\x04"
        self.assertTrue(self.device.connect())

    def test_sends_gps_request_after_interval(self):
        before = self.ecu.requests.count(b"\x21\x0B")
        self.clock.now += 1600
        self.assertTrue(self.device.keep_alive())
        self.assertEqual(self.ecu.requests.count(b"\x21\x0B"), before + 1)


class TestDisconnect(KWPTestCase):
    def setUp(self):
        super().setUp()
        self.connect_suzuki()

    def test_disconnect_sends_stop_and_waits(self):
        before = self.clock.now
        self.assertTrue(self.device.disconnect())
        self.assertIn(b"\x82", self.ecu.requests)
        self.assertFalse(self.device.connected)
        self.assertGreaterEqual(self.clock.now - before, 2000)

    def test_disconnect_already_disconnected(self):
        self.assertTrue(self.device.disconnect())
        self.assertTrue(self.device.disconnect())

    def test_disconnect_failure_sets_stop_error(self):
        self.ecu.silent = True
        self.assertFalse(self.device.disconnect())
        self.assertFalse(self.device.connected)
        self.assertTrue(self.device.error_flags & kwp2000.ERR_STOP)

    def test_disconnect_preserves_other_errors(self):
        self.device._error_flags |= kwp2000.ERR_USER
        self.assertTrue(self.device.disconnect())
        self.assertTrue(self.device.error_flags & kwp2000.ERR_USER)


# ---------------------------------------------------------------------------
# Errors, debug and dealer mode
# ---------------------------------------------------------------------------

class TestErrorsAndDebug(KWPTestCase):
    def test_error_flags_and_names(self):
        self.assertFalse(self.device.has_error)
        self.device._error_flags |= kwp2000.ERR_CONFIG
        self.assertTrue(self.device.has_error)
        self.assertIn("config", self.device.errors)

    def test_reset_error_clears_flags(self):
        self.device._error_flags = kwp2000.ERR_USER | kwp2000.ERR_CONFIG
        self.device.reset_error()
        self.assertEqual(self.device.error_flags, 0)

    def test_debug_is_off_by_default(self):
        self.assertFalse(self.device._debug_enabled)

    def test_debug_default_prints_connect(self):
        stream = StringIO()
        self.device.enable_debug(stream, kwp2000.KWP2000.DEBUG_DEFAULT)
        self.connect_suzuki()
        output = stream.getvalue()
        self.assertIn("Initialize K-line", output)
        self.assertIn("ECU connected", output)

    def test_debug_verbose_prints_frames(self):
        stream = StringIO()
        self.device.enable_debug(stream, kwp2000.KWP2000.DEBUG_VERBOSE)
        self.connect_suzuki()
        output = stream.getvalue()
        self.assertIn("Sending:", output)
        self.assertIn("Receiving:", output)

    def test_print_last_response_hex(self):
        stream = StringIO()
        self.device.enable_debug(stream, kwp2000.KWP2000.DEBUG_DEFAULT)
        self.connect_suzuki()
        self.device.print_last_response()
        self.assertIn("C1 EA 8F", stream.getvalue())

    def test_print_sensors_without_data_sets_user_error(self):
        stream = StringIO()
        self.device.enable_debug(stream, kwp2000.KWP2000.DEBUG_DEFAULT)
        self.device.print_sensors()
        self.assertTrue(self.device.error_flags & kwp2000.ERR_USER)


class TestDealerMode(KWPTestCase):
    def test_dealer_mode_is_none_before_enable(self):
        self.assertIsNone(self.device.dealer_mode)

    def test_set_before_enable_returns_false(self):
        self.assertFalse(self.device.set_dealer_mode(True))
        self.assertTrue(self.device.error_flags & kwp2000.ERR_USER)

    def test_suzuki_enable_and_toggle(self):
        dealer = FakePin(self.clock, value=0)
        self.assertTrue(self.device.enable_dealer_mode(dealer))
        self.assertFalse(self.device.dealer_mode)
        self.assertTrue(self.device.set_dealer_mode(True))
        self.assertTrue(self.device.dealer_mode)
        self.assertTrue(self.device.set_dealer_mode(False))
        self.assertFalse(self.device.dealer_mode)

    def test_non_suzuki_enable_returns_false(self):
        device = kwp2000.KWP2000(
            self.uart, "kawasaki", tx_pin=self.pin, rx_pin=16
        )
        self.assertFalse(device.enable_dealer_mode(FakePin(self.clock)))
        self.assertTrue(device.error_flags & kwp2000.ERR_USER)


class TestConstruction(unittest.TestCase):
    def test_brand_string_and_constant(self):
        device = kwp2000.KWP2000(FakeUART(FakeClock()), "Kawasaki")
        self.assertEqual(device.brand, kwp2000.KWP2000.KAWASAKI)
        self.assertEqual(device.brand_name, "kawasaki")

    def test_invalid_brand_raises(self):
        with self.assertRaises(ValueError):
            kwp2000.KWP2000(FakeUART(FakeClock()), "ducati")

    def test_uart_or_id_required(self):
        with self.assertRaises(ValueError):
            kwp2000.KWP2000(None, "suzuki")


if __name__ == "__main__":
    unittest.main()
