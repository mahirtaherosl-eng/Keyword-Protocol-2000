#!/usr/bin/env python3
"""Host tests for the BLE logger example (no hardware required).

Run from anywhere:

    python "micropython/examples/bluetooth android logger/tools/test_protocol.py"

The test stubs MicroPython-only modules, imports esp32_ble_logger.py, and
checks the pure protocol helpers plus cross-file UUID/CSV consistency.
"""

import importlib.util
import json
import os
import sys
import types
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE_DIR = os.path.dirname(HERE)
APP_PATH = os.path.join(EXAMPLE_DIR, "esp32_ble_logger.py")
MICROPYTHON_DIR = os.path.dirname(os.path.dirname(EXAMPLE_DIR))
KOTLIN_DIR = os.path.join(
    EXAMPLE_DIR, "android", "app", "src", "main", "java",
    "com", "example", "kwpblelogger",
)
RECEIVER_PATH = os.path.join(HERE, "ble_receiver.py")
STRINGS_PATH = os.path.join(
    EXAMPLE_DIR, "android", "app", "src", "main", "res", "values",
    "strings.xml",
)
README_PATH = os.path.join(EXAMPLE_DIR, "README.md")

CSV_HEADER = (
    "timestamp_iso,seq,uptime_ms,gps,clutch,rpm,speed,tps,"
    "stps,iap,iat,ect,voltage"
)


class FakeUuid:
    def __init__(self, value):
        self.value = value

    def __bytes__(self):
        return bytes(range(16))


class FakeBle:
    FLAG_READ = 2
    FLAG_WRITE = 8
    FLAG_NOTIFY = 16

    def __init__(self):
        pass

    def active(self, flag):
        pass

    def config(self, **kwargs):
        pass

    def irq(self, handler):
        pass

    def gatts_register_services(self, services):
        return ((1, 2),)

    def gap_advertise(self, *args, **kwargs):
        pass

    def gatts_notify(self, *args):
        pass


def install_stubs():
    bluetooth = types.ModuleType("bluetooth")
    bluetooth.UUID = FakeUuid
    bluetooth.BLE = FakeBle
    bluetooth.FLAG_READ = FakeBle.FLAG_READ
    bluetooth.FLAG_WRITE = FakeBle.FLAG_WRITE
    bluetooth.FLAG_NOTIFY = FakeBle.FLAG_NOTIFY
    sys.modules["bluetooth"] = bluetooth

    machine = types.ModuleType("machine")

    class Pin:
        def __init__(self, *args, **kwargs):
            pass

        def value(self, value=None):
            return 0

    class UART:
        def __init__(self, *args, **kwargs):
            pass

    machine.Pin = Pin
    machine.UART = UART
    sys.modules["machine"] = machine

    micropython = types.ModuleType("micropython")
    micropython.const = lambda value: value
    sys.modules["micropython"] = micropython


def load_app():
    install_stubs()
    if MICROPYTHON_DIR not in sys.path:
        sys.path.insert(0, MICROPYTHON_DIR)
    spec = importlib.util.spec_from_file_location("esp32_ble_logger", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


app = load_app()


class FakeTime:
    @staticmethod
    def ticks_ms():
        return 123456

    @staticmethod
    def ticks_add(a, b):
        return a + b

    @staticmethod
    def ticks_diff(a, b):
        return a - b

    @staticmethod
    def sleep_ms(ms):
        pass


app.time = FakeTime
DEFAULT_SENSORS = app.SUPPORTED_SENSOR_KEYS


class FakeEcu:
    sensors = {
        "gps": 3,
        "clutch": None,
        "rpm": 3125.456,
        "speed": 48,
        "tps": 14.234,
        "stps": None,
        "iap": 101.0,
        "iat": 26.5,
        "ect": 81.0,
        "voltage": None,
    }


class ProtocolTests(unittest.TestCase):
    def test_build_sample(self):
        raw = app.build_sample(FakeEcu(), 7)
        self.assertTrue(raw.endswith(b"\n"))
        sample = json.loads(raw.decode())
        self.assertEqual(
            list(sample.keys()),
            ["seq", "ms"] + list(app.SENSOR_KEYS),
        )
        self.assertEqual(sample["seq"], 7)
        self.assertEqual(sample["ms"], 123456)
        self.assertEqual(sample["rpm"], 3125.46)
        self.assertEqual(sample["tps"], 14.23)
        self.assertIsNone(sample["clutch"])
        self.assertIsNone(sample["voltage"])

    def test_build_sample_subset(self):
        raw = app.build_sample(FakeEcu(), 7, ("rpm", "speed"))
        sample = json.loads(raw.decode())
        self.assertEqual(
            list(sample.keys()), ["seq", "ms", "rpm", "speed"]
        )
        self.assertEqual(sample["rpm"], 3125.46)
        self.assertNotIn("ect", sample)

    def test_simulated_ecu_accepts_subset(self):
        ecu = app.FakeEcu()
        self.assertTrue(ecu.connect())
        self.assertTrue(ecu.request_sensors(only=("rpm", "speed")))
        self.assertTrue(ecu.keep_alive())

    def test_chunks_at_default_mtu(self):
        data = bytes(range(60))
        chunks = app.chunk_data(data, 23)
        self.assertEqual([len(chunk) for chunk in chunks], [20, 20, 20])
        self.assertEqual(b"".join(chunks), data)

    def test_chunks_at_large_mtu(self):
        data = bytes(range(60))
        chunks = app.chunk_data(data, 247)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], data)

    def test_advertising_payload(self):
        service_uuid = bytes(range(16))
        payload = app.adv_payload(service_uuid)
        self.assertEqual(len(payload), 21)
        self.assertEqual(payload[:3], b"\x02\x01\x06")
        self.assertEqual(payload[3], 0x11)
        self.assertEqual(payload[4], 0x07)
        self.assertEqual(payload[5:], service_uuid)

    def test_scan_response(self):
        payload = app.scan_response("KWP2000-C6")
        self.assertEqual(payload[0], 11)
        self.assertEqual(payload[1], 0x09)
        self.assertEqual(payload[2:].decode(), "KWP2000-C6")

    def test_control_valid(self):
        interval, sensors, ack = app.handle_control(
            b'{"cmd":"interval","ms":3000}\n', 2000, DEFAULT_SENSORS
        )
        self.assertEqual(interval, 3000)
        self.assertEqual(sensors, DEFAULT_SENSORS)
        self.assertEqual(ack["ack"], "interval")
        self.assertEqual(ack["ms"], 3000)
        self.assertEqual(ack["interval_ms"], 3000)
        self.assertEqual(ack["brand"], app.BRAND)
        self.assertEqual(
            ack["supported"], list(app.SUPPORTED_SENSOR_KEYS)
        )
        self.assertTrue(ack["ok"])

    def test_control_auto(self):
        interval, sensors, ack = app.handle_control(
            b'{"cmd":"interval","ms":0}', 2000, DEFAULT_SENSORS
        )
        self.assertEqual(interval, 0)
        self.assertEqual(sensors, DEFAULT_SENSORS)
        self.assertTrue(ack["ok"])

    def test_control_accepts_minimum(self):
        interval, sensors, ack = app.handle_control(
            b'{"cmd":"interval","ms":250}', 2000, DEFAULT_SENSORS
        )
        self.assertEqual(app.MIN_POLL_INTERVAL_MS, 250)
        self.assertEqual(interval, 250)
        self.assertTrue(ack["ok"])

    def test_control_clamps_below_minimum(self):
        interval, sensors, ack = app.handle_control(
            b'{"cmd":"interval","ms":249}', 2000, DEFAULT_SENSORS
        )
        self.assertEqual(interval, app.MIN_POLL_INTERVAL_MS)
        self.assertTrue(ack["ok"])

    def test_control_clamps_high(self):
        interval, sensors, ack = app.handle_control(
            b'{"cmd":"interval","ms":999999}', 2000, DEFAULT_SENSORS
        )
        self.assertEqual(interval, app.MAX_POLL_INTERVAL_MS)
        self.assertTrue(ack["ok"])

    def test_control_invalid_json(self):
        interval, sensors, ack = app.handle_control(
            b"not json", 2000, DEFAULT_SENSORS
        )
        self.assertEqual(interval, 2000)
        self.assertEqual(sensors, DEFAULT_SENSORS)
        self.assertFalse(ack["ok"])
        self.assertIn("error", ack)

    def test_control_unknown_command(self):
        interval, sensors, ack = app.handle_control(
            b'{"cmd":"reboot"}', 2000, DEFAULT_SENSORS
        )
        self.assertEqual(interval, 2000)
        self.assertFalse(ack["ok"])

    def test_control_invalid_ms(self):
        interval, sensors, ack = app.handle_control(
            b'{"cmd":"interval"}', 2000, DEFAULT_SENSORS
        )
        self.assertEqual(interval, 2000)
        self.assertFalse(ack["ok"])

    def test_control_sensors_filters_unsupported(self):
        interval, sensors, ack = app.handle_control(
            b'{"cmd":"sensors","keys":["rpm","speed","clutch"]}',
            2000, DEFAULT_SENSORS,
        )
        self.assertEqual(interval, 2000)
        self.assertEqual(sensors, ("rpm", "speed"))
        self.assertEqual(ack["sensors"], ["rpm", "speed"])
        self.assertTrue(ack["ok"])

    def test_control_rejects_no_supported_sensors(self):
        interval, sensors, ack = app.handle_control(
            b'{"cmd":"sensors","keys":["clutch","stps"]}',
            2000, DEFAULT_SENSORS,
        )
        self.assertEqual(interval, 2000)
        self.assertEqual(sensors, DEFAULT_SENSORS)
        self.assertFalse(ack["ok"])
        self.assertIn("error", ack)

    def test_control_config_auto_subset(self):
        interval, sensors, ack = app.handle_control(
            b'{"cmd":"config","interval_ms":0,'
            b'"sensors":["rpm","speed"]}',
            2000, DEFAULT_SENSORS,
        )
        self.assertEqual(interval, 0)
        self.assertEqual(sensors, ("rpm", "speed"))
        self.assertEqual(ack["ack"], "config")
        self.assertEqual(ack["interval_ms"], 0)
        self.assertEqual(ack["sensors"], ["rpm", "speed"])
        self.assertTrue(ack["ok"])

    def test_control_config_rejects_empty_sensors(self):
        interval, sensors, ack = app.handle_control(
            b'{"cmd":"config","interval_ms":0,"sensors":[]}',
            2000, DEFAULT_SENSORS,
        )
        self.assertEqual(interval, 2000)
        self.assertEqual(sensors, DEFAULT_SENSORS)
        self.assertFalse(ack["ok"])

    def test_kawasaki_supported_keys(self):
        self.assertEqual(
            app.SUPPORTED_SENSOR_KEYS,
            ("gps", "rpm", "speed", "tps", "iap", "iat", "ect"),
        )


class CrossFileTests(unittest.TestCase):
    def read(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_uuids_match_kotlin_and_receiver(self):
        esp32 = self.read(APP_PATH)
        kotlin = self.read(os.path.join(KOTLIN_DIR, "BleUuids.kt"))
        receiver = self.read(RECEIVER_PATH)
        for uuid in (app.SERVICE_UUID, app.SENSOR_UUID, app.CONTROL_UUID):
            self.assertIn(uuid, esp32)
            self.assertIn(uuid, kotlin)
            self.assertIn(uuid, receiver)

    def test_csv_header_matches_kotlin_and_receiver(self):
        kotlin = self.read(os.path.join(KOTLIN_DIR, "CsvLogger.kt"))
        receiver = self.read(RECEIVER_PATH)
        self.assertIn(CSV_HEADER, kotlin)
        self.assertIn(CSV_HEADER, receiver)

    def test_interval_options_match(self):
        kotlin = self.read(os.path.join(KOTLIN_DIR, "MainActivity.kt"))
        esp32 = self.read(APP_PATH)
        receiver = self.read(RECEIVER_PATH)
        readme = self.read(README_PATH)

        self.assertIn(
            "intArrayOf(0, 250, 500, 1000, 2000, 5000, 10000)", kotlin
        )
        self.assertIn("MIN_POLL_INTERVAL_MS = 250", esp32)
        self.assertIn("250-10000", receiver)
        self.assertIn("250-10000", readme)
        self.assertIn("Auto", readme)

        root = ET.parse(STRINGS_PATH).getroot()
        array = next(
            item for item in root.findall("string-array")
            if item.get("name") == "interval_options"
        )
        labels = [item.text for item in array.findall("item")]
        self.assertEqual(
            labels,
            ["Auto (fastest)", "0.25 s", "0.5 s", "1 s", "2 s", "5 s", "10 s"],
        )

    def test_sensor_keys_match_kotlin(self):
        kotlin = self.read(os.path.join(KOTLIN_DIR, "SensorKeys.kt"))
        for key in app.SENSOR_KEYS:
            self.assertIn('"%s"' % key, kotlin)


if __name__ == "__main__":
    unittest.main(verbosity=2)
