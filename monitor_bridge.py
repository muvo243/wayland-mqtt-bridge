#!/usr/bin/env python3
"""
monitor_bridge.py — expose a KDE/Wayland display's DPMS state as a
Home Assistant MQTT switch.

  * State:  kscreen-doctor --dpms show  ->  "on" / "off"
  * Off:    kscreen-doctor --dpms off   (DPMS standby; wakes on any user input)
  * On:     kscreen-doctor --dpms on

MQTT topics (defaults, overridable in config.json):
  homeassistant/switch/pc_monitor/config   (auto-discovery, published once)
  monitor/pc/state                         (retained: "ON" | "OFF")
  monitor/pc/set                           (command: "ON" | "OFF")
"""
import json
import os
import subprocess
import sys
import time

import paho.mqtt.client as mqtt

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
POLL_INTERVAL = 1.5

DEFAULTS = {
    "mqtt": {
        "host": "127.0.0.1",
        "port": 1883,
        "username": "",
        "password": "",
        "client_id": "monitor-bridge",
    },
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
    },
}


def _merge(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config():
    if not os.path.isfile(CONFIG_PATH):
        print(f"[bridge] missing {CONFIG_PATH}", file=sys.stderr)
        print("[bridge] copy config.example.json to config.json and edit it", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        user = json.load(f)
    return _merge(DEFAULTS, user)


def dpms_state():
    """Return 'on', 'off', or None if the query fails."""
    try:
        out = subprocess.run(
            ["kscreen-doctor", "--dpms", "show"],
            capture_output=True, text=True, timeout=10,
        )
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
    subprocess.run(
        ["kscreen-doctor", "--dpms", state],
        capture_output=True, text=True, timeout=15,
    )


def discovery_payload(cfg):
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
        "device": {
            "identifiers": [sw["device_id"]],
            "name": sw["device_name"],
            "manufacturer": sw["manufacturer"],
            "model": sw["model"],
            "sw_version": "mqtt-bridge-1.0",
        },
    }


def main():
    cfg = load_config()
    broker = cfg["mqtt"]
    topics = cfg["topics"]

    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=broker["client_id"])
    if broker.get("username"):
        cl.username_pw_set(broker["username"], broker.get("password", ""))
    cl.will_set(topics["state"], "OFF", qos=0, retain=True)

    connected = {}

    def on_connect(c, udata, flags, rc, props):
        print(f"[bridge] connected (rc={rc})")
        connected["ok"] = True
        c.publish(topics["discovery"], json.dumps(discovery_payload(cfg)), qos=1, retain=True)
        c.subscribe(topics["set"], qos=1)

    def on_message(c, udata, msg):
        cmd = msg.payload.decode().strip().upper()
        if cmd not in ("ON", "OFF"):
            return
        print(f"[bridge] command: {cmd}")
        dpms_set("off" if cmd == "OFF" else "on")

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

    last_published = None
    while True:
        state = dpms_state()
        if state is None:
            time.sleep(POLL_INTERVAL)
            continue
        mqtt_state = state.upper()
        if mqtt_state != last_published:
            print(f"[bridge] state: {mqtt_state}")
            cl.publish(topics["state"], mqtt_state, qos=1, retain=True)
            last_published = mqtt_state
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] in ("on", "off"):
        dpms_set(argv[0])
        print(dpms_state())
    elif argv and argv[0] == "state":
        print(dpms_state())
    else:
        main()
