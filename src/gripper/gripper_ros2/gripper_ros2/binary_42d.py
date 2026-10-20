"""42D-70 open/close protocol behind the existing gripper interface.

The reported aperture is an endpoint in the configured legacy units, not a
position measurement. Opening the bus and observing status never write to the
controller. Serial mode is prepared only by an explicit enable or queued move.
"""

from __future__ import annotations

import math
import time

from .gripper_driver import GripperBus, GripperDevice, _empty_feedback

try:
    import minimalmodbus
    import serial
except ImportError:  # Hardware dependencies are optional for fake-transport tests.
    minimalmodbus = None
    serial = None


REG_CONTROL_MODE = 0x0035
REG_OPEN = 0x0036
REG_CLOSE = 0x0037
REG_STATUS = 0x0040
STATUS_OPEN = 6
STATUS_CLOSED = 1
STATUS_CLOSED_ALT = 2


def _check_threshold(threshold, hysteresis):
    if (not math.isfinite(threshold) or not math.isfinite(hysteresis)
            or not 0 < threshold < 1
            or not 0 <= hysteresis < min(threshold, 1 - threshold)):
        raise ValueError("Invalid binary gripper threshold/hysteresis")


class Binary42DRTU:
    """One exclusive serial handle and one instrument for the whole bus.

    The owning bus holds its lock while switching ``instrument.address`` and
    calling these methods. The settling interval applies after *every* register
    transaction, including a failed transaction and a slave address change.
    """

    def __init__(self, port, baudrate=115200, timeout=0.3,
                 inter_transaction_s=0.1):
        self.port, self.baudrate, self.timeout = port, int(baudrate), float(timeout)
        self.inter_transaction_s = max(0.1, float(inter_transaction_s))
        self.instrument = None
        self._serial = None
        self._last_io_at = None

    def connect(self):
        if self._serial is not None and self._serial.is_open:
            return True
        if minimalmodbus is None or serial is None:
            raise RuntimeError("minimalmodbus and pyserial are required for hardware access")
        handle = serial.Serial(
            self.port, baudrate=self.baudrate, bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout, write_timeout=self.timeout, exclusive=True,
        )
        try:
            self.instrument = minimalmodbus.Instrument(handle, 1, mode=minimalmodbus.MODE_RTU)
            self.instrument.clear_buffers_before_each_transaction = True
            self.instrument.close_port_after_each_call = False
        except Exception:
            handle.close()
            raise
        self._serial = handle
        return True

    def disconnect(self):
        try:
            if self._serial is not None:
                self._serial.close()
        finally:
            self._serial = self.instrument = None

    def _io(self, operation, *args, **kwargs):
        if self._last_io_at is not None:
            remaining = self.inter_transaction_s - (time.monotonic() - self._last_io_at)
            if remaining > 0:
                time.sleep(remaining)
        try:
            return operation(*args, **kwargs)
        finally:
            self._last_io_at = time.monotonic()

    def read_register(self, address):
        return self._io(self.instrument.read_register, address, functioncode=3)

    def write_register(self, address, value):
        return self._io(self.instrument.write_register, address, value, functioncode=16)


class Binary42DDevice(GripperDevice):
    is_binary = True

    def __init__(self, *args, threshold=0.5, hysteresis=0.05, **kwargs):
        super().__init__(*args, **kwargs)
        self.threshold, self.hysteresis = float(threshold), float(hysteresis)
        _check_threshold(self.threshold, self.hysteresis)
        self._clear_session()

    def _clear_session(self):
        self._enabled = self._prepared = self._feedback_connected = False
        self._desired_target = self._sent_target = self._motion_target = None
        self._observed_target = None
        self._last_error = None
        self.feedback = _empty_feedback()
        # No physical aperture is available before an endpoint is observed.
        position = (self.open_position + self.close_position) // 2
        self.feedback.update(position=position, position_mm=0.0,
                             raw_status=None, position_known=False)

    @property
    def connected(self):
        return self.bus.connected and self._feedback_connected

    def enable(self, on=True):
        with self.bus._lock:
            if not on:
                # The documented protocol has no disable/stop register. This
                # gates future driver writes without claiming to stop motion.
                self._enabled = self._prepared = False
                self._desired_target = self._sent_target = self._motion_target = None
                self.bus._clear_commands(self.slave_id)
                return
            self._enabled = self._prepared = False

            def _prepare(sdk):
                if sdk.read_register(REG_CONTROL_MODE) != 1:
                    sdk.write_register(REG_CONTROL_MODE, 1)
                if sdk.read_register(REG_CONTROL_MODE) != 1:
                    raise RuntimeError("42D-70 did not enter serial control mode")

            try:
                self.bus.transaction(self.slave_id, _prepare)
            except Exception as error:
                self._read_failed(error)
                raise
            self._enabled = self._prepared = True

    def reset(self):
        raise NotImplementedError("42D-70 reset is not supported by the documented protocol")

    def _select_target(self, position):
        """Update hysteresis for every input, even when a queue coalesces it."""
        position = float(position)
        if not math.isfinite(position):
            raise ValueError("Gripper target must be finite")
        position = max(self.min_position, min(self.max_position, position))
        span = self.open_position - self.close_position
        if not span:
            raise ValueError("Binary gripper open/close endpoints must differ")
        opening = max(0.0, min(1.0, (position - self.close_position) / span))
        boundary = self.threshold
        if self._desired_target is True:
            boundary -= self.hysteresis
        elif self._desired_target is False:
            boundary += self.hysteresis
        self._desired_target = opening >= boundary
        return self._desired_target

    def _endpoint(self, target):
        return self.open_position if target else self.close_position

    def _command(self, target):
        """Send a selected endpoint with the bus lock held; never retry writes."""
        if not self._enabled or not self._prepared:
            raise RuntimeError("42D-70 must be enabled and serial mode verified before motion")
        if self._sent_target is target:
            return False
        try:
            self.bus.transaction(
                self.slave_id,
                lambda sdk: sdk.write_register(REG_OPEN if target else REG_CLOSE, 1),
            )
        except Exception as error:
            # A timed out command might have reached hardware. Only a new
            # caller request may issue another command; it is never replayed.
            self._read_failed(error)
            raise
        self._sent_target = self._motion_target = target
        self.feedback["position_reached"] = False
        return True

    def move_to(self, position, speed_pct=None, force_pct=None):
        with self.bus._lock:
            self.set_params(speed_pct, force_pct)
            with self.bus._cmd_lock:
                target = self._select_target(position)
                self.bus._pending.pop(self.slave_id, None)
                self.bus._last_request.pop(self.slave_id, None)
            sent = self._command(target)
            if sent:
                self.bus._last_sent[self.slave_id] = (time.time(), self._endpoint(target))
            return sent

    def _read_failed(self, error):
        with self.bus._lock:
            self._feedback_connected = self._enabled = self._prepared = False
            self._desired_target = self._sent_target = self._motion_target = None
            self.bus._clear_commands(self.slave_id)
            self._last_error = str(error)
            self.feedback.update(ready=False, position_reached=False, torque_reached=False)

    def read_feedback(self):
        try:
            with self.bus._lock:
                status = self.bus.transaction(self.slave_id, lambda sdk: sdk.read_register(REG_STATUS))
                observed = True if status == STATUS_OPEN else False if status in (
                    STATUS_CLOSED, STATUS_CLOSED_ALT) else None
                if observed is not None:
                    self._observed_target = observed
                position = (self.feedback["position"] if self._observed_target is None
                            else self._endpoint(self._observed_target))
                self.feedback = {
                    "position": position,
                    "position_mm": round(position / self.pos_unit_scale, 2) if self.pos_unit_scale else 0.0,
                    "speed": 0, "current": 0,
                    "torque_reached": False,
                    "position_reached": observed is not None and (
                        self._motion_target is None or observed is self._motion_target),
                    "speed_max_reached": False, "ready": observed is not None,
                    "alarm": 0, "ts": time.time(), "raw_status": status,
                    "position_known": observed is not None,
                }
                self._feedback_connected, self._last_error = True, None
                return self.feedback
        except Exception as error:
            self._read_failed(error)
            raise

    def wait_until_reached(self, timeout=20.0):
        with self.bus._lock:
            target = self._motion_target
        if target is None:
            return "timeout"
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            with self.bus._lock:
                if not self._enabled or self._motion_target is not target:
                    return "timeout"
                feedback = self.read_feedback()
                status = feedback["raw_status"]
                if target and status == STATUS_OPEN:
                    return "position"
                if not target and status in (STATUS_CLOSED, STATUS_CLOSED_ALT):
                    return "position"
            # Release the bus between observations so the other slave can move.
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return "timeout"

    def snapshot(self):
        result = super().snapshot()
        result.update(connected=self.connected, error=self._last_error,
                      protocol="binary_42d", position_is_synthetic=True)
        return result


class Binary42DBus(GripperBus):
    def __init__(self, *args, inter_transaction_s=0.1, binary_threshold=0.5,
                 binary_hysteresis=0.05, **kwargs):
        super().__init__(*args, **kwargs)
        self.inter_transaction_s = float(inter_transaction_s)
        if not math.isfinite(self.inter_transaction_s) or self.inter_transaction_s < 0.1:
            raise ValueError("42D-70 requires at least 0.1 seconds between transactions")
        self.binary_threshold, self.binary_hysteresis = float(binary_threshold), float(binary_hysteresis)
        _check_threshold(self.binary_threshold, self.binary_hysteresis)

    def add_gripper(self, slave_id, **kwargs):
        kwargs.setdefault("threshold", self.binary_threshold)
        kwargs.setdefault("hysteresis", self.binary_hysteresis)
        device = Binary42DDevice(self, slave_id, **kwargs)
        self.devices[int(slave_id)] = device
        return device

    def _ensure_sdk(self):
        if self._sdk is None:
            self._sdk = Binary42DRTU(self.port, baudrate=self.baudrate, timeout=self.timeout,
                                     inter_transaction_s=self.inter_transaction_s)
        return self._sdk

    def transaction(self, slave_id, fn):
        with self._lock:
            if not self._connected:
                raise RuntimeError("42D-70 serial bus is not connected")
            sdk = self._ensure_sdk()
            sdk.instrument.address = int(slave_id)
            return fn(sdk)

    def _clear_commands(self, slave_id):
        with self._cmd_lock:
            self._pending.pop(slave_id, None)
            self._last_sent.pop(slave_id, None)
            self._last_request.pop(slave_id, None)

    def _clear_session(self):
        with self._cmd_lock:
            self._pending.clear()
            self._last_sent.clear()
            self._last_request.clear()
            for device in self.devices.values():
                device._clear_session()

    def connect(self):
        with self._lock:
            if self._connected:
                return True
            self._clear_session()
            try:
                self._connected = bool(self._ensure_sdk().connect())
                self._last_error = None if self._connected else "Could not open serial bus"
            except Exception as error:
                self._connected, self._last_error = False, str(error)
                if self._sdk is not None:
                    self._sdk.disconnect()
                self._sdk = None
            return self._connected

    def disconnect(self):
        with self._lock:
            self._clear_session()
            try:
                if self._sdk is not None:
                    self._sdk.disconnect()
            finally:
                self._connected, self._sdk = False, None

    def _try_reconnect(self):
        self.disconnect()
        return self.connect()

    def request_move(self, slave_id, position, now=None):
        slave_id = int(slave_id)
        with self._cmd_lock:
            device = self.get(slave_id)
            target = device._select_target(position)
            self._pending[slave_id] = device._endpoint(target)
            self._last_request[slave_id] = time.time() if now is None else now
        self._wake.set()

    def _process_pending(self, now=None):
        now = time.time() if now is None else now
        sent = False
        with self._cmd_lock:
            pending = dict(self._pending)
        for slave_id, position in pending.items():
            with self._lock:
                device = self.devices.get(slave_id)
                if device is None:
                    self._drop_pending(slave_id, position)
                    continue
                with self._cmd_lock:
                    # A service, disable or reconnect may have invalidated the
                    # snapshot while this worker was waiting for the bus lock.
                    if self._pending.get(slave_id) != position:
                        continue
                target = position == device.open_position
                last = self._last_sent.get(slave_id)
                if device._sent_target is target:
                    self._drop_pending(slave_id, position)
                    continue
                if last is not None and now - last[0] < self.min_command_interval_s:
                    continue
                try:
                    if not device._enabled or not device._prepared:
                        device.enable(True)
                    # Preparation can take several serial transactions. Keep
                    # coalescing inputs until it finishes, then commit the
                    # newest endpoint before beginning the motion write.
                    with self._cmd_lock:
                        position = self._pending.pop(slave_id, None)
                    if position is None:
                        continue
                    target = position == device.open_position
                    if device._command(target):
                        self._last_sent[slave_id] = (now, position)
                        sent = True
                except Exception as error:
                    device._read_failed(error)
                    # Do not replay an uncertain write, or starve the other slave.
        return sent

    def apply_endpoints(self, slave_id, open_position, close_position):
        with self._lock:
            super().apply_endpoints(slave_id, open_position, close_position)
            device = self.get(slave_id)
            device._desired_target = device._sent_target = device._motion_target = None
            if device._observed_target is not None:
                position = device._endpoint(device._observed_target)
                device.feedback.update(position=position, position_mm=(
                    round(position / device.pos_unit_scale, 2) if device.pos_unit_scale else 0.0))
            device.feedback["position_reached"] = False

    def _poll_devices(self):
        if not self.polling_enabled:
            return
        devices = ([self.get(self.active_slave_id)] if self.active_slave_id in self.devices
                   else list(self.devices.values()))
        for device in devices:
            try:
                device.read_feedback()
            except Exception:
                pass  # Each device records its own failure; keep polling its peer.

    def _run(self):
        period = 1.0 / max(1.0, self.poll_hz)
        previous = time.time()
        next_retry = previous + self.reconnect_interval if self._connected else 0.0
        while self._running:
            started = time.time()
            if self._connected:
                try:
                    self._cycle(started)
                except Exception as error:
                    self._last_error = str(error)
                errors = [f"{d.name}: {d._last_error}" for d in self.devices.values() if d._last_error]
                self._last_error = "; ".join(errors) or None
            all_offline = self.polling_enabled and self.devices and not any(
                device.connected for device in self.devices.values())
            if self.auto_reconnect and (not self._connected or all_offline) and time.time() >= next_retry:
                next_retry = time.time() + self.reconnect_interval
                self._try_reconnect()
            dt = started - previous
            self._hz = round(1.0 / dt, 1) if self._connected and dt > 1e-6 else 0.0
            previous = started
            due = self._next_due(time.time())
            wait = period - (time.time() - started)
            self._wake.wait(max(0.0, wait if due is None else min(wait, due)))
            self._wake.clear()

    def stop(self):
        self._running = False
        self._wake.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
