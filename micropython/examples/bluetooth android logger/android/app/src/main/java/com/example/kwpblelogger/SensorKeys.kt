package com.example.kwpblelogger

import org.json.JSONArray
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

    /** Parse a config ACK; returns null for sensor samples or bad JSON. */
    fun parseAck(line: String): Ack? {
        return try {
            val json = JSONObject(line)
            if (!json.has("ack")) return null
            Ack(
                command = json.optString("ack"),
                ok = json.optBoolean("ok", false),
                intervalMs = when {
                    json.has("interval_ms") -> json.optInt("interval_ms")
                    json.has("ms") -> json.optInt("ms")
                    else -> null
                },
                sensors = jsonArrayToList(json.optJSONArray("sensors")),
                supported = jsonArrayToList(json.optJSONArray("supported")),
                brand = if (json.isNull("brand")) null else json.optString("brand")
                    .takeIf { it.isNotEmpty() },
                error = if (json.isNull("error")) null else json.optString("error")
                    .takeIf { it.isNotEmpty() }
            )
        } catch (e: Exception) {
            null
        }
    }

    private fun jsonArrayToList(array: JSONArray?): List<String>? {
        if (array == null) return null
        val values = ArrayList<String>(array.length())
        for (i in 0 until array.length()) {
            values.add(array.optString(i))
        }
        return values
    }
}

data class SensorSample(
    val seq: Long,
    val uptimeMs: Long,
    val values: Map<String, Double?>
)

data class Ack(
    val command: String,
    val ok: Boolean,
    val intervalMs: Int?,
    val sensors: List<String>?,
    val supported: List<String>?,
    val brand: String?,
    val error: String?
)
