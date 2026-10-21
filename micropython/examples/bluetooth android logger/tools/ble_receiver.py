#!/usr/bin/env python3
"""Optional PC receiver for the ESP32 BLE KWP2000 logger.

Install the one dependency, then run:

    pip install bleak
    python "micropython/examples/bluetooth android logger/tools/ble_receiver.py"

It scans for the KWP2000 service, prints every sensor sample, and can write a
CSV file in the same format as the Android app. This is useful for separating
ESP32 problems from Android problems.

Examples:
    .../ble_receiver.py --duration 30
    .../ble_receiver.py --csv pc_log.csv --duration 60
    .../ble_receiver.py --interval 5000 --duration 30
    .../ble_receiver.py --address AA:BB:CC:DD:EE:FF
"""

import argparse
import asyncio
import csv
import datetime
import json
import sys

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover - documented optional dependency
    BleakClient = None
    BleakScanner = None

SERVICE_UUID = "6b773230-2d4b-5750-8000-6b7770323030"
SENSOR_UUID = "6b773230-2d4b-5750-8001-6b7770323030"
CONTROL_UUID = "6b773230-2d4b-5750-8002-6b7770323030"

SENSOR_KEYS = (
    "gps", "clutch", "rpm", "speed", "tps",
    "stps", "iap", "iat", "ect", "voltage",
)
CSV_HEADER = "timestamp_iso,seq,uptime_ms,gps,clutch,rpm,speed,tps,stps,iap,iat,ect,voltage"


def _format_number(value):
    if value is None:
        return ""
    if isinstance(value, float):
        text = ("%.4f" % value).rstrip("0").rstrip(".")
        return text if text else "0"
    return str(value)


class CsvSink:
    def __init__(self, path):
        self.handle = open(path, "w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.handle)
        self.writer.writerow(CSV_HEADER.split(","))

    def write(self, sample):
        timestamp = datetime.datetime.now().astimezone().isoformat(
            timespec="milliseconds"
        )
        row = [timestamp, sample.get("seq", ""), sample.get("ms", "")]
        row.extend(_format_number(sample.get(key)) for key in SENSOR_KEYS)
        self.writer.writerow(row)
        self.handle.flush()

    def close(self):
        self.handle.close()


async def find_device(args):
    if args.address:
        return args.address
    if BleakScanner is None:
        raise RuntimeError("bleak is not installed; run: pip install bleak")
    print("Scanning for the KWP2000 service...")
    devices = await BleakScanner.discover(
        timeout=args.scan_timeout, service_uuids=[SERVICE_UUID]
    )
    if not devices:
        raise RuntimeError(
            "No KWP2000 BLE device found. Is the ESP32 running and advertising?"
        )
    device = devices[0]
    print("Found %s (%s)" % (device.name, device.address))
    return device


async def run(args):
    if BleakClient is None:
        raise RuntimeError("bleak is not installed; run: pip install bleak")

    target = await find_device(args)
    sink = CsvSink(args.csv) if args.csv else None
    buffer = bytearray()

    def handle_notification(_sender, data):
        buffer.extend(data)
        while b"\n" in buffer:
            raw, _, rest = buffer.partition(b"\n")
            buffer[:] = rest
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                print("Bad JSON:", line)
                continue
            if "ack" in obj:
                print("ACK:", obj)
                continue
            print(
                "seq=%-5s ms=%-8s rpm=%-7s speed=%-5s tps=%-5s ect=%-5s"
                % (
                    obj.get("seq"),
                    obj.get("ms"),
                    obj.get("rpm"),
                    obj.get("speed"),
                    obj.get("tps"),
                    obj.get("ect"),
                )
            )
            if sink is not None:
                sink.write(obj)

    print("Connecting to %s ..." % (target,))
    try:
        async with BleakClient(target) as client:
            await client.start_notify(SENSOR_UUID, handle_notification)
            print("Subscribed to sensor notifications.")
            if args.interval:
                payload = ('{"cmd":"interval","ms":%d}\n' % args.interval).encode()
                await client.write_gatt_char(
                    CONTROL_UUID, payload, response=True
                )
                print("Requested interval:", args.interval, "ms")
            if args.duration > 0:
                await asyncio.sleep(args.duration)
            else:
                print("Press Ctrl-C to stop.")
                try:
                    while True:
                        await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    pass
            await client.stop_notify(SENSOR_UUID)
    finally:
        if sink is not None:
            sink.close()
            print("CSV written to", args.csv)


def main():
    parser = argparse.ArgumentParser(
        description="Receive KWP2000 sensor samples over BLE."
    )
    parser.add_argument("--address", help="skip scanning and use this address")
    parser.add_argument(
        "--scan-timeout", type=float, default=10.0,
        help="scan duration in seconds (default 10)",
    )
    parser.add_argument(
        "--duration", type=float, default=0,
        help="stop after N seconds (default 0 = until Ctrl-C)",
    )
    parser.add_argument("--csv", help="write samples to this CSV file")
    parser.add_argument(
        "--interval", type=int,
        help="write an update-interval command (1000-10000 ms)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped")
    except RuntimeError as exc:
        print("Error:", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
