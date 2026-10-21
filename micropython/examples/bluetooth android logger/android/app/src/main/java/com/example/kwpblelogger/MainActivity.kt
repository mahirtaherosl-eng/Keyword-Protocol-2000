package com.example.kwpblelogger

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothManager
import android.content.ClipData
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.view.View
import android.view.WindowManager
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.ListView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.FileProvider
import java.io.File
import java.util.Locale

class MainActivity : ComponentActivity(), BleSensorClient.Listener {

    private lateinit var statusText: TextView
    private lateinit var detailText: TextView
    private lateinit var scanButton: Button
    private lateinit var disconnectButton: Button
    private lateinit var logButton: Button
    private lateinit var shareButton: Button
    private lateinit var intervalSpinner: Spinner
    private lateinit var devicesLabel: TextView
    private lateinit var scanList: ListView
    private lateinit var lastSampleText: TextView
    private lateinit var valueViews: Map<String, TextView>

    private lateinit var bleClient: BleSensorClient
    private lateinit var csvLogger: CsvLogger
    private lateinit var deviceAdapter: ArrayAdapter<String>

    private val scanDevices = ArrayList<BleSensorClient.ScanEntry>()
    private val intervalOptions = intArrayOf(1000, 2000, 5000, 10000)

    @Volatile private var currentState = BleSensorClient.State.IDLE
    private var logging = false
    private var currentLogFile: File? = null

    private val prefs by lazy {
        getSharedPreferences("settings", Context.MODE_PRIVATE)
    }

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { result ->
        if (result.values.all { it }) {
            ensureBluetoothEnabled()
        } else {
            showError("Bluetooth permissions are required to scan.")
        }
    }

    private val enableBtLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == RESULT_OK) {
            beginScan()
        } else {
            showError("Bluetooth must be enabled to scan.")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        statusText = findViewById(R.id.statusText)
        detailText = findViewById(R.id.detailText)
        scanButton = findViewById(R.id.scanButton)
        disconnectButton = findViewById(R.id.disconnectButton)
        logButton = findViewById(R.id.logButton)
        shareButton = findViewById(R.id.shareButton)
        intervalSpinner = findViewById(R.id.intervalSpinner)
        devicesLabel = findViewById(R.id.devicesLabel)
        scanList = findViewById(R.id.scanList)
        lastSampleText = findViewById(R.id.lastSampleText)

        valueViews = mapOf(
            "gps" to findViewById<TextView>(R.id.value_gps),
            "clutch" to findViewById<TextView>(R.id.value_clutch),
            "rpm" to findViewById<TextView>(R.id.value_rpm),
            "speed" to findViewById<TextView>(R.id.value_speed),
            "tps" to findViewById<TextView>(R.id.value_tps),
            "stps" to findViewById<TextView>(R.id.value_stps),
            "iap" to findViewById<TextView>(R.id.value_iap),
            "iat" to findViewById<TextView>(R.id.value_iat),
            "ect" to findViewById<TextView>(R.id.value_ect),
            "voltage" to findViewById<TextView>(R.id.value_voltage)
        )

        bleClient = BleSensorClient(this, this)
        csvLogger = CsvLogger(this)

        deviceAdapter = ArrayAdapter(
            this, android.R.layout.simple_list_item_1, ArrayList<String>()
        )
        scanList.adapter = deviceAdapter
        scanList.setOnItemClickListener { _, _, position, _ ->
            val entry = scanDevices.getOrNull(position) ?: return@setOnItemClickListener
            scanDevices.clear()
            deviceAdapter.clear()
            scanList.visibility = View.GONE
            devicesLabel.visibility = View.GONE
            bleClient.connect(entry.device)
        }

        scanButton.setOnClickListener { requestPermissionsAndScan() }
        disconnectButton.setOnClickListener {
            stopLogging()
            bleClient.disconnect()
        }
        logButton.setOnClickListener {
            if (logging) stopLogging() else startLogging()
        }
        shareButton.setOnClickListener { shareCsv() }

        val savedInterval = prefs.getInt(KEY_INTERVAL, DEFAULT_INTERVAL_MS)
        intervalSpinner.setSelection(
            intervalOptions.indexOf(savedInterval).coerceAtLeast(0), false
        )
        intervalSpinner.onItemSelectedListener =
            object : AdapterView.OnItemSelectedListener {
                override fun onItemSelected(
                    parent: AdapterView<*>?,
                    view: View?,
                    position: Int,
                    id: Long
                ) {
                    val ms = intervalOptions[
                        position.coerceIn(0, intervalOptions.lastIndex)
                    ]
                    prefs.edit().putInt(KEY_INTERVAL, ms).apply()
                    bleClient.setInterval(ms)
                }

                override fun onNothingSelected(parent: AdapterView<*>?) = Unit
            }

        bleClient.setInterval(selectedIntervalMs())
        updateUiForState(
            BleSensorClient.State.IDLE,
            getString(R.string.status_detail_idle)
        )
        shareButton.isEnabled = csvLogger.latestFile() != null
    }

    private fun requestPermissionsAndScan() {
        val missing = requiredPermissions().filter {
            checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) {
            ensureBluetoothEnabled()
        } else {
            permissionLauncher.launch(missing.toTypedArray())
        }
    }

    private fun requiredPermissions(): List<String> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            listOf(
                Manifest.permission.BLUETOOTH_SCAN,
                Manifest.permission.BLUETOOTH_CONNECT
            )
        } else {
            listOf(Manifest.permission.ACCESS_FINE_LOCATION)
        }

    @SuppressLint("MissingPermission")
    private fun ensureBluetoothEnabled() {
        val adapter =
            (getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter
        if (adapter == null) {
            showError("This device has no Bluetooth adapter.")
            return
        }
        val enabled = try {
            adapter.isEnabled
        } catch (e: SecurityException) {
            false
        }
        if (!enabled) {
            try {
                enableBtLauncher.launch(Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE))
            } catch (e: SecurityException) {
                showError("Bluetooth permission is required to enable Bluetooth.")
            }
            return
        }
        if (Build.VERSION.SDK_INT <= Build.VERSION_CODES.R && !isLocationEnabled()) {
            showError(
                "Enable location services to scan for BLE devices on Android 11 and below."
            )
            return
        }
        beginScan()
    }

    private fun isLocationEnabled(): Boolean {
        val manager =
            getSystemService(Context.LOCATION_SERVICE) as? LocationManager ?: return true
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            manager.isLocationEnabled
        } else {
            manager.isProviderEnabled(LocationManager.GPS_PROVIDER) ||
                manager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)
        }
    }

    private fun beginScan() {
        scanDevices.clear()
        deviceAdapter.clear()
        devicesLabel.visibility = View.VISIBLE
        scanList.visibility = View.VISIBLE
        bleClient.startScan()
    }

    override fun onScanResults(entries: List<BleSensorClient.ScanEntry>) {
        for (entry in entries) {
            if (scanDevices.any { it.address == entry.address }) continue
            scanDevices.add(entry)
            deviceAdapter.add(
                "${entry.name}\n${entry.address}   RSSI ${entry.rssi} dBm"
            )
        }
    }

    override fun onStateChanged(state: BleSensorClient.State, detail: String) {
        currentState = state
        updateUiForState(state, detail)
        if (state != BleSensorClient.State.SCANNING) {
            devicesLabel.visibility = View.GONE
            scanList.visibility = View.GONE
        }
        if (state == BleSensorClient.State.DISCONNECTED && logging) {
            stopLogging()
        }
    }

    private fun updateUiForState(
        state: BleSensorClient.State,
        detail: String
    ) {
        val label = when (state) {
            BleSensorClient.State.IDLE -> "Idle"
            BleSensorClient.State.SCANNING -> "Scanning"
            BleSensorClient.State.CONNECTING -> "Connecting"
            BleSensorClient.State.DISCOVERING -> "Discovering"
            BleSensorClient.State.NEGOTIATING_MTU -> "Negotiating MTU"
            BleSensorClient.State.ENABLING_NOTIFY -> "Subscribing"
            BleSensorClient.State.READY -> "Connected"
            BleSensorClient.State.DISCONNECTED -> "Disconnected"
            BleSensorClient.State.ERROR -> "Error"
        }
        statusText.text = label
        detailText.text = detail
        val ready = state == BleSensorClient.State.READY
        scanButton.isEnabled = state == BleSensorClient.State.IDLE ||
            state == BleSensorClient.State.DISCONNECTED ||
            state == BleSensorClient.State.ERROR
        disconnectButton.isEnabled = state != BleSensorClient.State.IDLE
        logButton.isEnabled = ready
        intervalSpinner.isEnabled = ready
    }

    override fun onSample(sample: SensorSample) {
        for ((key, view) in valueViews) {
            val value = sample.values[key]
            view.text = if (value == null) {
                getString(R.string.value_none)
            } else {
                formatValue(value)
            }
        }
        lastSampleText.text = getString(
            R.string.last_sample_format, sample.seq, sample.uptimeMs
        )
        if (logging) csvLogger.append(sample)
    }

    override fun onAck(ack: Ack) {
        val ackMs = ack.ms
        detailText.text = if (ack.ok && ackMs != null) {
            getString(R.string.ack_applied, ackMs / 1000.0)
        } else {
            getString(R.string.ack_failed, ack.error ?: "unknown")
        }
    }

    override fun onError(message: String) {
        showError(message)
    }

    private fun startLogging() {
        if (currentState != BleSensorClient.State.READY) {
            showError(getString(R.string.not_connected))
            return
        }
        csvLogger.start { file ->
            runOnUiThread {
                if (file == null) {
                    showError(getString(R.string.log_failed))
                    return@runOnUiThread
                }
                currentLogFile = file
                logging = true
                logButton.text = getString(R.string.stop_logging)
                shareButton.isEnabled = false
                window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                detailText.text = getString(R.string.log_started, file.name)
            }
        }
    }

    private fun stopLogging() {
        if (!logging) return
        logging = false
        logButton.text = getString(R.string.start_logging)
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        csvLogger.stop { file, rows ->
            runOnUiThread {
                currentLogFile = file
                shareButton.isEnabled = file != null
                detailText.text = if (file != null) {
                    getString(R.string.log_saved, file.name, rows)
                } else {
                    getString(R.string.log_failed)
                }
            }
        }
    }

    private fun shareCsv() {
        val file = currentLogFile ?: csvLogger.latestFile()
        if (file == null || !file.exists()) {
            showError(getString(R.string.no_log))
            return
        }
        val uri = FileProvider.getUriForFile(
            this, "$packageName.fileprovider", file
        )
        val send = Intent(Intent.ACTION_SEND).apply {
            type = "text/csv"
            putExtra(Intent.EXTRA_STREAM, uri)
            putExtra(Intent.EXTRA_SUBJECT, file.name)
            clipData = ClipData.newRawUri(file.name, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        startActivity(Intent.createChooser(send, getString(R.string.share_chooser)))
    }

    private fun selectedIntervalMs(): Int =
        intervalOptions[
            intervalSpinner.selectedItemPosition.coerceIn(
                0, intervalOptions.lastIndex
            )
        ]

    private fun formatValue(value: Double): String {
        if (!value.isFinite()) return getString(R.string.value_none)
        return if (value == value.toLong().toDouble()) {
            value.toLong().toString()
        } else {
            String.format(Locale.US, "%.2f", value)
        }
    }

    private fun showError(message: String) {
        detailText.text = message
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    }

    override fun onDestroy() {
        logging = false
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        bleClient.close()
        csvLogger.shutdown()
        super.onDestroy()
    }

    private companion object {
        const val KEY_INTERVAL = "interval_ms"
        const val DEFAULT_INTERVAL_MS = 2000
    }
}
