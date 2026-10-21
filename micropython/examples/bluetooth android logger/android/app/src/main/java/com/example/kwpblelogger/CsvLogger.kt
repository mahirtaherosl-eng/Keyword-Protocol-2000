package com.example.kwpblelogger

import android.content.Context
import java.io.BufferedWriter
import java.io.File
import java.io.FileWriter
import java.math.BigDecimal
import java.time.Instant
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import java.util.concurrent.Executors

/**
 * Writes one CSV row per sample on a single background thread.
 *
 * start/append/stop are queued in order, so the header always comes first and
 * the file is closed after the last row. Every row is flushed immediately.
 */
class CsvLogger(private val context: Context) {

    private val executor = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "csv-logger").apply { isDaemon = true }
    }

    @Volatile private var writer: BufferedWriter? = null
    @Volatile private var currentFile: File? = null
    @Volatile private var rows: Long = 0

    private val isoFormat = DateTimeFormatter
        .ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", Locale.US)
        .withZone(ZoneId.systemDefault())
    private val nameFormat =
        DateTimeFormatter.ofPattern("yyyyMMdd_HHmmss", Locale.US)

    fun start(onReady: (File?) -> Unit) {
        executor.execute {
            closeWriter()
            val dir = logDir()
            if (!dir.exists() && !dir.mkdirs()) {
                onReady(null)
                return@execute
            }
            val file = File(
                dir,
                "kwp2000_" + nameFormat.format(LocalDateTime.now()) + ".csv"
            )
            try {
                val out = BufferedWriter(FileWriter(file, false))
                out.write(HEADER)
                out.newLine()
                out.flush()
                writer = out
                currentFile = file
                rows = 0
                onReady(file)
            } catch (e: Exception) {
                writer = null
                currentFile = null
                onReady(null)
            }
        }
    }

    fun append(sample: SensorSample) {
        executor.execute {
            val out = writer ?: return@execute
            val cells = ArrayList<String>(3 + SensorKeys.ALL.size)
            cells.add(isoFormat.format(Instant.now()))
            cells.add(sample.seq.toString())
            cells.add(sample.uptimeMs.toString())
            for (key in SensorKeys.ALL) {
                cells.add(formatNumber(sample.values[key]))
            }
            try {
                out.write(cells.joinToString(","))
                out.newLine()
                out.flush()
                rows++
            } catch (e: Exception) {
                // Keep the samples already written; stop() reports the file.
            }
        }
    }

    fun stop(onClosed: (File?, Long) -> Unit) {
        executor.execute {
            val file = currentFile
            val count = rows
            closeWriter()
            onClosed(file, count)
        }
    }

    fun shutdown() {
        try {
            executor.execute { closeWriter() }
        } catch (e: Exception) {
            // Executor already shut down.
        }
        executor.shutdown()
    }

    fun latestFile(): File? {
        val dir = logDir()
        return dir.listFiles { file ->
            file.isFile && file.name.endsWith(".csv")
        }?.maxByOrNull { file -> file.lastModified() }
    }

    private fun logDir(): File =
        context.getExternalFilesDir("logs") ?: File(context.filesDir, "logs")

    private fun closeWriter() {
        val out = writer
        writer = null
        try {
            out?.flush()
            out?.close()
        } catch (e: Exception) {
            // Rows are flushed after every append, so this only loses the close.
        }
    }

    companion object {
        const val HEADER = "timestamp_iso,seq,uptime_ms,gps,clutch,rpm,speed,tps,stps,iap,iat,ect,voltage"

        private fun formatNumber(value: Double?): String {
            if (value == null || !value.isFinite()) return ""
            return BigDecimal.valueOf(value).stripTrailingZeros().toPlainString()
        }
    }
}
