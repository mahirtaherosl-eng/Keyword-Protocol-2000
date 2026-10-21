package com.example.kwpblelogger

import java.util.UUID

/** UUIDs and names shared with esp32_ble_logger.py. */
object BleUuids {
    val SERVICE: UUID = UUID.fromString("6b773230-2d4b-5750-8000-6b7770323030")
    val SENSOR: UUID = UUID.fromString("6b773230-2d4b-5750-8001-6b7770323030")
    val CONTROL: UUID = UUID.fromString("6b773230-2d4b-5750-8002-6b7770323030")
    val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

    const val DEVICE_NAME = "KWP2000-C6"
}
