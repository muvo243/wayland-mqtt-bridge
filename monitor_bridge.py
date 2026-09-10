#!/usr/bin/env python3
"""
monitor_bridge.py — expose a KDE/Wayland display's DPMS power,
brightness, and user-idle time as Home Assistant MQTT entities.

  * State:       kscreen-doctor --dpms show  ->  "on" / "off"
  * Off:         kscreen-doctor --dpms off   (standby; wakes on any user input)
  * On:          kscreen-doctor --dpms on
  * Brightness:  kscreen-doctor -j  /  output.<name>.brightness.<0-100>
  * Idle:        last keyboard/mouse/touchpad event from /dev/input
"""
import array
import datetime
import fcntl
import glob
import json
import os
import select
import struct
import subprocess
import sys
import threading
import time

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
POLL_INTERVAL = 1.5
IDLE_AFTER_S = 5.0

DEFAULTS = {
    "mqtt": {
        "host": "127.0.0.1",
        "port": 1883,
        "username": "",
        "password": "",
        "client_id": "monitor-bridge",
    },
    "output": "",
    "switch": {
        "name": "PC Monitor",
        "object_id": "pc_monitor",
        "unique_id": "pc_monitor_dpms_switch",
        "manufacturer": "Generic",
        "model": "Display",
        "icon": "mdi:monitor",
        "device_id": "pc-monitor-bridge",
        "device_name": "PC Monitor",
    },
    "topics": {
        "discovery": "homeassistant/switch/pc_monitor/config",
        "state": "monitor/pc/state",
        "set": "monitor/pc/set",
        "brightness_discovery": "homeassistant/number/pc_monitor_brightness/config",
        "brightness_state": "monitor/pc/brightness",
        "brightness_set": "monitor/pc/brightness/set",
        "last_input_discovery": "homeassistant/sensor/pc_monitor_last_input/config",
        "last_input_state": "monitor/pc/last_input",
        "idle_discovery": "homeassistant/sensor/pc_monitor_idle/config",
        "idle_state": "monitor/pc/idle",
    },
    "idle_after": 5,
}

EV_KEY, EV_REL, EV_ABS = 1, 2, 3
INPUT_EVENT = struct.Struct("llHHi")
EVENT_SIZE = INPUT_EVENT.size
INPUT_SCAN_INTERVAL = 15.0
SKIP_INPUT_NAMES = (
    "lid switch",
    "video bus",
    "pc speaker",
    "headphone",
    "hdmi",
    "privacy driver",
    "sof-hda",
)


def _merge(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(required=True):
    if not os.path.isfile(CONFIG_PATH):
        if required:
            print(f"[bridge] missing {CONFIG_PATH}", file=sys.stderr)
            print("[bridge] copy config.example.json to config.json and edit it", file=sys.stderr)
            sys.exit(1)
        return dict(DEFAULTS)
    with open(CONFIG_PATH) as f:
        user = json.load(f)
    return _merge(DEFAULTS, user)


def _run(args, timeout=10):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def dpms_state():
    """Return 'on', 'off', or None if the query fails."""
    try:
        out = _run(["kscreen-doctor", "--dpms", "show"], timeout=10)
        text = (out.stdout + out.stderr).lower()
        if "off" in text:
            return "off"
        if "on" in text:
            return "on"
        return None
    except Exception as e:
        print(f"[bridge] dpms query failed: {e}", file=sys.stderr)
        return None


def dpms_set(state: str):
    _run(["kscreen-doctor", "--dpms", state], timeout=15)


def kscreen_config():
    try:
        out = _run(["kscreen-doctor", "-j"], timeout=10)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout)
    except Exception as e:
        print(f"[bridge] kscreen json failed: {e}", file=sys.stderr)
        return None


def pick_output(cfg, data=None):
    if data is None:
        data = kscreen_config()
    if not data:
        return None
    outputs = data.get("outputs") or []
    wanted = (cfg.get("output") or "").strip()
    if wanted:
        for o in outputs:
            if o.get("name") == wanted:
                return o
        print(f"[bridge] output {wanted!r} not found", file=sys.stderr)
        return None
    for o in outputs:
        if o.get("enabled") and o.get("connected") and o.get("brightness") is not None:
            return o
    return None


def brightness_get(cfg):
    """Return brightness 0-100, or None."""
    o = pick_output(cfg)
    if not o or o.get("brightness") is None:
        return None
    try:
        return int(round(float(o["brightness"]) * 100))
    except (TypeError, ValueError):
        return None


def brightness_set(cfg, percent):
    try:
        percent = int(round(float(percent)))
    except (TypeError, ValueError):
        print(f"[bridge] invalid brightness: {percent!r}", file=sys.stderr)
        return False
    percent = max(0, min(100, percent))
    o = pick_output(cfg)
    if not o or not o.get("name"):
        print("[bridge] no output for brightness", file=sys.stderr)
        return False
    name = o["name"]
    try:
        out = _run(["kscreen-doctor", f"output.{name}.brightness.{percent}"], timeout=15)
        if out.returncode != 0:
            err = (out.stderr or out.stdout).strip()
            print(f"[bridge] brightness set failed: {err}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"[bridge] brightness set failed: {e}", file=sys.stderr)
        return False


def _ioc_read(nr, size, itype=ord("E")):
    return (2 << 30) | (itype << 8) | nr | (size << 16)


def _evdev_name(fd):
    buf = array.array("B", [0] * 256)
    fcntl.ioctl(fd, _ioc_read(0x06, 256), buf, True)
    return buf.tobytes().split(b"\x00", 1)[0].decode("utf-8", "replace")


def _evdev_type_bits(fd):
    buf = array.array("B", [0] * 16)
    fcntl.ioctl(fd, _ioc_read(0x20, 16), buf, True)
    return buf


def _has_ev(bits, ev):
    return bool(bits[ev // 8] & (1 << (ev % 8)))


def iso_utc(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ActivityMonitor:
    """Track last keyboard/mouse/touchpad event via evdev."""

    def __init__(self, idle_after=IDLE_AFTER_S, on_resume=None):
        self._lock = threading.Lock()
        self._last = time.time()
        self._idle_after = float(idle_after)
        self._on_resume = on_resume
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="input-idle", daemon=True)

    def start(self):
        self._thread.start()

    def set_on_resume(self, cb):
        self._on_resume = cb

    def last_input(self):
        with self._lock:
            return self._last

    def idle_seconds(self):
        return max(0.0, time.time() - self.last_input())

    def last_input_iso(self):
        return iso_utc(self.last_input())

    def _mark(self, ts):
        resume = False
        with self._lock:
            if ts <= self._last:
                return
            if (ts - self._last) >= self._idle_after:
                resume = True
            self._last = ts
        if resume:
            cb = self._on_resume
            if cb:
                try:
                    cb(ts)
                except Exception as e:
                    print(f"[bridge] resume callback failed: {e}", file=sys.stderr)

    def _open_devices(self):
        fds = {}
        for path in glob.glob("/dev/input/event*"):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            try:
                name = _evdev_name(fd).lower()
                if any(s in name for s in SKIP_INPUT_NAMES):
                    os.close(fd)
                    continue
                bits = _evdev_type_bits(fd)
                if not (_has_ev(bits, EV_KEY) or _has_ev(bits, EV_REL) or _has_ev(bits, EV_ABS)):
                    os.close(fd)
                    continue
                fds[fd] = path
            except OSError:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return fds

    def _run(self):
        fds = {}
        last_scan = 0.0
        try:
            while not self._stop.is_set():
                now = time.time()
                if now - last_scan >= INPUT_SCAN_INTERVAL or not fds:
                    for fd in list(fds):
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    fds = self._open_devices()
                    last_scan = now
                    if not fds:
                        print("[bridge] no readable input devices for idle tracking", file=sys.stderr)
                if not fds:
                    self._stop.wait(1.0)
                    continue
                try:
                    ready, _, _ = select.select(list(fds), [], [], 1.0)
                except (OSError, ValueError):
                    fds = {}
                    last_scan = 0.0
                    continue
                for fd in ready:
                    try:
                        data = os.read(fd, EVENT_SIZE * 64)
                    except BlockingIOError:
                        continue
                    except OSError:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        fds.pop(fd, None)
                        continue
                    for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                        sec, usec, typ, _code, _value = INPUT_EVENT.unpack_from(data, off)
                        if typ not in (EV_KEY, EV_REL, EV_ABS):
                            continue
                        ts = sec + usec / 1_000_000
                        if ts < 1_000_000_000:
                            ts = time.time()
                        self._mark(ts)
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass


def ha_device(cfg):
    sw = cfg["switch"]
    return {
        "identifiers": [sw["device_id"]],
        "name": sw["device_name"],
        "manufacturer": sw["manufacturer"],
        "model": sw["model"],
        "sw_version": "mqtt-bridge-1.2",
    }


def switch_discovery_payload(cfg):
    sw = cfg["switch"]
    topics = cfg["topics"]
    return {
        "name": sw["name"],
        "object_id": sw["object_id"],
        "unique_id": sw["unique_id"],
        "command_topic": topics["set"],
        "state_topic": topics["state"],
        "payload_on": "ON",
        "payload_off": "OFF",
        "value_template": "{{ value }}",
        "icon": sw["icon"],
        "device": ha_device(cfg),
    }


def brightness_discovery_payload(cfg):
    sw = cfg["switch"]
    topics = cfg["topics"]
    return {
        "name": f"{sw['name']} Brightness",
        "object_id": f"{sw['object_id']}_brightness",
        "unique_id": f"{sw['unique_id']}_brightness",
        "command_topic": topics["brightness_set"],
        "state_topic": topics["brightness_state"],
        "min": 0,
        "max": 100,
        "step": 1,
        "mode": "slider",
        "unit_of_measurement": "%",
        "icon": "mdi:brightness-6",
        "device": ha_device(cfg),
    }


def last_input_discovery_payload(cfg):
    sw = cfg["switch"]
    topics = cfg["topics"]
    return {
        "name": f"{sw['name']} Last Input",
        "object_id": f"{sw['object_id']}_last_input",
        "unique_id": f"{sw['unique_id']}_last_input",
        "state_topic": topics["last_input_state"],
        "device_class": "timestamp",
        "icon": "mdi:gesture-tap",
        "device": ha_device(cfg),
    }


def main():
    import paho.mqtt.client as mqtt

    cfg = load_config()
    broker = cfg["mqtt"]
    topics = cfg["topics"]
    idle_after = float(cfg.get("idle_after", IDLE_AFTER_S))

    activity = ActivityMonitor(idle_after=idle_after)
    activity.start()

    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=broker["client_id"])
    if broker.get("username"):
        cl.username_pw_set(broker["username"], broker.get("password", ""))
    cl.will_set(topics["state"], "OFF", qos=0, retain=True)

    connected = {}

    def on_connect(c, udata, flags, rc, props):
        print(f"[bridge] connected (rc={rc})")
        connected["ok"] = True
        c.publish(topics["discovery"], json.dumps(switch_discovery_payload(cfg)), qos=1, retain=True)
        c.publish(
            topics["brightness_discovery"],
            json.dumps(brightness_discovery_payload(cfg)),
            qos=1,
            retain=True,
        )
        c.publish(
            topics["last_input_discovery"],
            json.dumps(last_input_discovery_payload(cfg)),
            qos=1,
            retain=True,
        )
        # Drop the old per-second idle sensor if it was discovered previously.
        if topics.get("idle_discovery"):
            c.publish(topics["idle_discovery"], "", qos=1, retain=True)
        if topics.get("idle_state"):
            c.publish(topics["idle_state"], "", qos=1, retain=True)
        c.subscribe(topics["set"], qos=1)

    def on_message(c, udata, msg):
        payload = msg.payload.decode().strip()
        if msg.topic == topics["set"]:
            cmd = payload.upper()
            if cmd not in ("ON", "OFF"):
                return
            print(f"[bridge] command: {cmd}")
            dpms_set("off" if cmd == "OFF" else "on")
        elif msg.topic == topics["brightness_set"]:
            print(f"[bridge] brightness command: {payload}")
            brightness_set(cfg, payload)

    cl.on_connect = on_connect
    cl.on_message = on_message

    cl.connect(broker["host"], int(broker["port"]))
    cl.loop_start()

    for _ in range(60):
        if connected.get("ok"):
            break
        time.sleep(0.5)
    else:
        print("[bridge] could not connect to MQTT broker", file=sys.stderr)
        sys.exit(1)

    def on_resume(ts):
        iso = iso_utc(ts)
        print(f"[bridge] last input (resume): {iso}")
        cl.publish(topics["last_input_state"], iso, qos=1, retain=True)

    activity.set_on_resume(on_resume)

    last_power = None
    last_brightness = None
    was_idle = False
    brightness_cmd_subscribed = False
    while True:
        state = dpms_state()
        if state is not None:
            mqtt_state = state.upper()
            if mqtt_state != last_power:
                print(f"[bridge] state: {mqtt_state}")
                cl.publish(topics["state"], mqtt_state, qos=1, retain=True)
                last_power = mqtt_state

        bri = brightness_get(cfg)
        if bri is not None and bri != last_brightness:
            print(f"[bridge] brightness: {bri}")
            cl.publish(topics["brightness_state"], str(bri), qos=1, retain=True)
            last_brightness = bri
        if bri is not None and not brightness_cmd_subscribed:
            cl.subscribe(topics["brightness_set"], qos=1)
            brightness_cmd_subscribed = True

        idle = activity.idle_seconds() >= idle_after
        if idle and not was_idle:
            iso = activity.last_input_iso()
            print(f"[bridge] last input (idle): {iso}")
            cl.publish(topics["last_input_state"], iso, qos=1, retain=True)
            was_idle = True
        elif not idle:
            was_idle = False

        time.sleep(POLL_INTERVAL)


def cli(argv):
    if argv and argv[0] in ("on", "off"):
        dpms_set(argv[0])
        print(dpms_state())
        return
    if argv and argv[0] == "state":
        print(dpms_state())
        return
    if argv and argv[0] == "brightness":
        cfg = load_config(required=False)
        if len(argv) == 1:
            print(brightness_get(cfg))
        else:
            brightness_set(cfg, argv[1])
            print(brightness_get(cfg))
        return
    if argv and argv[0] == "idle":
        mon = ActivityMonitor()
        mon.start()
        time.sleep(2)
        print(int(mon.idle_seconds()))
        print(mon.last_input_iso())
        return
    main()


if __name__ == "__main__":
    cli(sys.argv[1:])
