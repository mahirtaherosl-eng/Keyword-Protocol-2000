package com.example.kwpblelogger

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import java.io.ByteArrayOutputStream
import java.nio.charset.StandardCharsets

/**
 * Scans for, connects to, and reads the ESP32 BLE sensor service.
 *
 * All callbacks are posted to the main thread. Only one GATT operation is in
 * flight at a time: discover -> MTU -> CCCD -> notifications/writes.
 */
class BleSensorClient(
    private val context: Context,
    private val listener: Listener
) {

    enum class State {
        IDLE, SCANNING, CONNECTING, DISCOVERING, NEGOTIATING_MTU,
        ENABLING_NOTIFY, READY, DISCONNECTED, ERROR
    }

    data class ScanEntry(
        val device: BluetoothDevice,
        val name: String,
        val address: String,
        val rssi: Int
    )

    interface Listener {
        fun onScanResults(entries: List<ScanEntry>)
        fun onStateChanged(state: State, detail: String)
        fun onSample(sample: SensorSample)
        fun onAck(ack: Ack)
        fun onError(message: String)
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private val adapter: BluetoothAdapter? =
        (context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter

    @Volatile private var scannerActive = false
    @Volatile private var gatt: BluetoothGatt? = null
    @Volatile private var sensorChar: BluetoothGattCharacteristic? = null
    @Volatile private var controlChar: BluetoothGattCharacteristic? = null
    @Volatile private var state = State.IDLE
    @Volatile private var autoReconnect = false
    @Volatile private var lastDevice: BluetoothDevice? = null
    @Volatile private var reconnectAttempt = 0
    @Volatile private var pendingIntervalMs: Int? = null
    @Volatile private var lastIntervalMs: Int? = null
    @Volatile private var writeInFlight = false
    @Volatile private var negotiatedMtu = 23

    private val rxLock = Any()
    private val rxBuffer = ByteArrayOutputStream()
    private val newline = '\n'.code.toByte()

    private val scanCallback = object : ScanCallback() {
        @SuppressLint("MissingPermission")
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val device = result.device
            val name = try {
                device.name
            } catch (e: SecurityException) {
                null
            } ?: result.scanRecord?.deviceName ?: BleUuids.DEVICE_NAME
            val address = try {
                device.address
            } catch (e: SecurityException) {
                ""
            }
            val entry = ScanEntry(device, name, address, result.rssi)
            mainHandler.post { listener.onScanResults(listOf(entry)) }
        }

        override fun onScanFailed(errorCode: Int) {
            scannerActive = false
            mainHandler.removeCallbacks(scanTimeout)
            mainHandler.post {
                listener.onError("BLE scan failed (code $errorCode)")
                setState(State.ERROR, "Scan failed")
            }
        }
    }

    private val mtuFallback = Runnable {
        if (state == State.NEGOTIATING_MTU) {
            val current = gatt
            if (current != null) enableNotifications(current)
        }
    }

    private val writeTimeout = Runnable {
        writeInFlight = false
        val next = pendingIntervalMs
        if (next != null) sendInterval(next)
    }

    private val scanTimeout = Runnable {
        if (scannerActive) {
            stopScan()
            setState(State.DISCONNECTED, "Scan stopped")
        }
    }

    private val reconnectRunnable = Runnable {
        val device = lastDevice
        if (autoReconnect && device != null) connectInternal(device)
    }

    private val gattCallback = object : BluetoothGattCallback() {
        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(
            gatt: BluetoothGatt,
            status: Int,
            newState: Int
        ) {
            if (gatt !== this@BleSensorClient.gatt) return
            if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                val reconnect = autoReconnect
                closeGatt()
                if (reconnect) {
                    setState(State.DISCONNECTED, "Device disconnected; reconnecting")
                    scheduleReconnect()
                } else {
                    setState(State.DISCONNECTED, "Disconnected")
                }
                return
            }
            if (status != BluetoothGatt.GATT_SUCCESS) {
                fail("GATT error $status")
                return
            }
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                setState(State.DISCOVERING, "Discovering services")
                if (!gatt.discoverServices()) fail("Service discovery could not start")
            }
        }

        override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
            if (gatt !== this@BleSensorClient.gatt) return
            if (status != BluetoothGatt.GATT_SUCCESS) {
                fail("Service discovery failed ($status)")
                return
            }
            val service = gatt.getService(BleUuids.SERVICE)
            if (service == null) {
                fail("KWP2000 service not found")
                return
            }
            val sensor = service.getCharacteristic(BleUuids.SENSOR)
            val control = service.getCharacteristic(BleUuids.CONTROL)
            if (sensor == null || control == null) {
                fail("KWP2000 characteristics not found")
                return
            }
            if ((sensor.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY) == 0) {
                fail("Sensor characteristic does not notify")
                return
            }
            if ((control.properties and BluetoothGattCharacteristic.PROPERTY_WRITE) == 0) {
                fail("Control characteristic is not writable")
                return
            }
            sensorChar = sensor
            controlChar = control
            setState(State.NEGOTIATING_MTU, "Negotiating MTU")
            if (!gatt.requestMtu(REQUESTED_MTU)) {
                enableNotifications(gatt)
            } else {
                mainHandler.postDelayed(mtuFallback, MTU_TIMEOUT_MS)
            }
        }

        override fun onMtuChanged(gatt: BluetoothGatt, mtu: Int, status: Int) {
            if (gatt !== this@BleSensorClient.gatt) return
            mainHandler.removeCallbacks(mtuFallback)
            negotiatedMtu = if (status == BluetoothGatt.GATT_SUCCESS) mtu else 23
            if (state == State.NEGOTIATING_MTU) enableNotifications(gatt)
        }

        override fun onDescriptorWrite(
            gatt: BluetoothGatt,
            descriptor: BluetoothGattDescriptor,
            status: Int
        ) {
            if (gatt !== this@BleSensorClient.gatt) return
            if (descriptor.uuid != BleUuids.CCCD) return
            if (status != BluetoothGatt.GATT_SUCCESS) {
                fail("Notification subscription failed ($status)")
                return
            }
            setState(State.READY, "Connected · MTU $negotiatedMtu")
            reconnectAttempt = 0
            val next = lastIntervalMs
            if (next != null) sendInterval(next)
        }

        override fun onCharacteristicWrite(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            status: Int
        ) {
            if (gatt !== this@BleSensorClient.gatt) return
            if (characteristic.uuid != BleUuids.CONTROL) return
            mainHandler.removeCallbacks(writeTimeout)
            writeInFlight = false
            if (status != BluetoothGatt.GATT_SUCCESS) {
                mainHandler.post { listener.onError("Interval write failed ($status)") }
            }
            val next = pendingIntervalMs
            if (next != null) sendInterval(next)
        }

        override fun onCharacteristicChanged(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray
        ) {
            if (gatt !== this@BleSensorClient.gatt) return
            if (characteristic.uuid == BleUuids.SENSOR) handleChunk(value)
        }

        @Suppress("DEPRECATION")
        override fun onCharacteristicChanged(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic
        ) {
            if (gatt !== this@BleSensorClient.gatt) return
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) return
            val value = characteristic.value ?: return
            if (characteristic.uuid == BleUuids.SENSOR) handleChunk(value)
        }
    }

    @SuppressLint("MissingPermission")
    fun startScan() {
        if (scannerActive) return
        // A manual scan replaces any pending auto-reconnect.
        autoReconnect = false
        mainHandler.removeCallbacks(reconnectRunnable)
        val bluetoothAdapter = adapter
        if (bluetoothAdapter == null) {
            listener.onError("This device has no Bluetooth adapter.")
            return
        }
        if (!hasScanPermission()) {
            listener.onError("Bluetooth scan permission is missing.")
            return
        }
        val enabled = try {
            bluetoothAdapter.isEnabled
        } catch (e: SecurityException) {
            false
        }
        if (!enabled) {
            listener.onError("Bluetooth is off.")
            return
        }
        val scanner = bluetoothAdapter.bluetoothLeScanner
        if (scanner == null) {
            listener.onError("BLE scanning is not available.")
            return
        }
        val filter = ScanFilter.Builder()
            .setServiceUuid(ParcelUuid(BleUuids.SERVICE))
            .build()
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()
        scannerActive = true
        setState(State.SCANNING, "Scanning for ${BleUuids.DEVICE_NAME}")
        try {
            scanner.startScan(listOf(filter), settings, scanCallback)
            mainHandler.removeCallbacks(scanTimeout)
            mainHandler.postDelayed(scanTimeout, SCAN_TIMEOUT_MS)
        } catch (e: SecurityException) {
            scannerActive = false
            listener.onError("Bluetooth scan permission is missing.")
            setState(State.ERROR, "Scan failed")
        }
    }

    @SuppressLint("MissingPermission")
    fun stopScan() {
        mainHandler.removeCallbacks(scanTimeout)
        if (!scannerActive) return
        scannerActive = false
        try {
            adapter?.bluetoothLeScanner?.stopScan(scanCallback)
        } catch (e: SecurityException) {
            // Permission revoked; nothing else to do.
        }
    }

    @SuppressLint("MissingPermission")
    fun connect(device: BluetoothDevice) {
        if (!hasConnectPermission()) {
            listener.onError("Bluetooth connect permission is missing.")
            return
        }
        stopScan()
        autoReconnect = true
        lastDevice = device
        reconnectAttempt = 0
        connectInternal(device)
    }

    fun disconnect() {
        autoReconnect = false
        mainHandler.removeCallbacks(mtuFallback)
        mainHandler.removeCallbacks(writeTimeout)
        mainHandler.removeCallbacks(reconnectRunnable)
        stopScan()
        closeGatt()
        setState(State.DISCONNECTED, "Disconnected")
    }

    fun close() {
        autoReconnect = false
        stopScan()
        mainHandler.removeCallbacksAndMessages(null)
        closeGatt()
    }

    /** Send the update interval; stored and retried if not READY yet. */
    fun setInterval(ms: Int) {
        lastIntervalMs = ms
        pendingIntervalMs = ms
        sendInterval(ms)
    }

    @SuppressLint("MissingPermission")
    private fun connectInternal(device: BluetoothDevice) {
        closeGatt()
        setState(State.CONNECTING, "Connecting to ${safeName(device)}")
        gatt = try {
            device.connectGatt(
                context, false, gattCallback, BluetoothDevice.TRANSPORT_LE
            )
        } catch (e: SecurityException) {
            null
        }
        if (gatt == null) {
            setState(State.ERROR, "connectGatt failed")
            scheduleReconnect()
        }
    }

    @SuppressLint("MissingPermission")
    private fun closeGatt() {
        val current = gatt
        gatt = null
        sensorChar = null
        controlChar = null
        writeInFlight = false
        try {
            current?.disconnect()
        } catch (e: SecurityException) {
            // Ignore; close() still releases the client.
        }
        try {
            current?.close()
        } catch (e: SecurityException) {
            // Ignore.
        }
    }

    @SuppressLint("MissingPermission")
    private fun enableNotifications(gatt: BluetoothGatt) {
        val characteristic = sensorChar
        if (characteristic == null) {
            fail("Sensor characteristic missing")
            return
        }
        if (!gatt.setCharacteristicNotification(characteristic, true)) {
            fail("Could not enable notifications")
            return
        }
        val cccd = characteristic.getDescriptor(BleUuids.CCCD)
        if (cccd == null) {
            fail("CCCD descriptor missing")
            return
        }
        setState(State.ENABLING_NOTIFY, "Subscribing")
        val started = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            // 0 is BluetoothStatusCodes.SUCCESS (API 33).
            gatt.writeDescriptor(
                cccd, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            ) == 0
        } else {
            @Suppress("DEPRECATION")
            cccd.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            @Suppress("DEPRECATION")
            gatt.writeDescriptor(cccd)
        }
        if (!started) fail("CCCD write failed")
    }

    @SuppressLint("MissingPermission")
    private fun sendInterval(ms: Int) {
        val currentGatt = gatt
        val characteristic = controlChar
        if (currentGatt == null || characteristic == null || state != State.READY) {
            pendingIntervalMs = ms
            return
        }
        if (writeInFlight) {
            pendingIntervalMs = ms
            return
        }
        val payload = "{\"cmd\":\"interval\",\"ms\":$ms}\n"
            .toByteArray(StandardCharsets.UTF_8)
        val started = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            // 0 is BluetoothStatusCodes.SUCCESS (API 33).
            currentGatt.writeCharacteristic(
                characteristic,
                payload,
                BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            ) == 0
        } else {
            @Suppress("DEPRECATION")
            characteristic.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            @Suppress("DEPRECATION")
            characteristic.value = payload
            @Suppress("DEPRECATION")
            currentGatt.writeCharacteristic(characteristic)
        }
        if (started) {
            writeInFlight = true
            pendingIntervalMs = null
            mainHandler.postDelayed(writeTimeout, WRITE_TIMEOUT_MS)
        } else {
            pendingIntervalMs = ms
            mainHandler.postDelayed({
                val next = pendingIntervalMs
                if (next != null) sendInterval(next)
            }, WRITE_RETRY_MS)
        }
    }

    private fun scheduleReconnect() {
        if (!autoReconnect) return
        val device = lastDevice ?: return
        val delayMs = (RECONNECT_BASE_MS shl minOf(reconnectAttempt, 5))
            .coerceAtMost(RECONNECT_MAX_MS)
        reconnectAttempt++
        setState(State.DISCONNECTED, "Reconnecting in ${delayMs / 1000} s")
        mainHandler.removeCallbacks(reconnectRunnable)
        mainHandler.postDelayed(reconnectRunnable, delayMs)
    }

    private fun fail(message: String) {
        mainHandler.post { listener.onError(message) }
        closeGatt()
        if (autoReconnect) {
            setState(State.DISCONNECTED, message)
            scheduleReconnect()
        } else {
            setState(State.ERROR, message)
        }
    }

    private fun setState(newState: State, detail: String) {
        state = newState
        mainHandler.post { listener.onStateChanged(newState, detail) }
    }

    private fun handleChunk(value: ByteArray) {
        synchronized(rxLock) {
            if (rxBuffer.size() + value.size > MAX_RX_BUFFER) {
                rxBuffer.reset()
                mainHandler.post { listener.onError("RX buffer overflow") }
                return
            }
            rxBuffer.write(value)
            val bytes = rxBuffer.toByteArray()
            var start = 0
            for (i in bytes.indices) {
                if (bytes[i] == newline) {
                    val line = String(
                        bytes, start, i - start, StandardCharsets.UTF_8
                    ).trim()
                    if (line.isNotEmpty()) dispatchLine(line)
                    start = i + 1
                }
            }
            rxBuffer.reset()
            if (start < bytes.size) {
                rxBuffer.write(bytes, start, bytes.size - start)
            }
        }
    }

    private fun dispatchLine(line: String) {
        val ack = SensorKeys.parseAck(line)
        if (ack != null) {
            mainHandler.post { listener.onAck(ack) }
            return
        }
        val sample = SensorKeys.parse(line)
        if (sample != null) {
            mainHandler.post { listener.onSample(sample) }
        } else {
            mainHandler.post { listener.onError("Bad JSON: ${line.take(60)}") }
        }
    }

    private fun hasScanPermission(): Boolean =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            context.checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) ==
                PackageManager.PERMISSION_GRANTED
        } else {
            context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) ==
                PackageManager.PERMISSION_GRANTED
        }

    private fun hasConnectPermission(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.S ||
            context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) ==
            PackageManager.PERMISSION_GRANTED

    private fun safeName(device: BluetoothDevice): String =
        try {
            device.name ?: device.address
        } catch (e: SecurityException) {
            "device"
        }

    private companion object {
        const val REQUESTED_MTU = 247
        const val MTU_TIMEOUT_MS = 2000L
        const val SCAN_TIMEOUT_MS = 10000L
        const val WRITE_TIMEOUT_MS = 2000L
        const val WRITE_RETRY_MS = 500L
        const val MAX_RX_BUFFER = 8192
        const val RECONNECT_BASE_MS = 1000L
        const val RECONNECT_MAX_MS = 30000L
    }
}
