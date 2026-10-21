package com.example.kwpblelogger

import org.json.JSONObject

/** Sensor keys, JSON parsing and the two line types sent by the ESP32. */
object SensorKeys {
    val ALL = listOf(
        "gps", "clutch", "rpm", "speed", "tps",
        "stps", "iap", "iat", "ect", "voltage"
    )

    /** Parse a sensor sample; returns null for ACK/control lines or bad JSON. */
    fun parse(line: String): SensorSample? {
        return try {
            val json = JSONObject(line)
            if (json.has("ack")) return null
            val values = HashMap<String, Double?>()
            for (key in ALL) {
                values[key] = if (json.isNull(key)) null else json.optDouble(key)
            }
            SensorSample(
                seq = json.optLong("seq", -1L),
                uptimeMs = json.optLong("ms", -1L),
                values = values
            )
        } catch (e: Exception) {
            null
        }
    }

    /** Parse an interval ACK; returns null for sensor samples or bad JSON. */
    fun parseAck(line: String): Ack? {
        return try {
            val json = JSONObject(line)
            if (!json.has("ack")) return null
            Ack(
                ok = json.optBoolean("ok", false),
                ms = if (json.has("ms")) json.optInt("ms") else null,
                error = if (json.isNull("error")) null else json.optString("error")
                    .takeIf { it.isNotEmpty() }
            )
        } catch (e: Exception) {
            null
        }
    }
}

data class SensorSample(
    val seq: Long,
    val uptimeMs: Long,
    val values: Map<String, Double?>
)

data class Ack(
    val ok: Boolean,
    val ms: Int?,
    val error: String?
)
