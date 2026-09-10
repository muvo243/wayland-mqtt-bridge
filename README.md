# wayland-mqtt-bridge

MQTT bridge that exposes a KDE/Wayland display's DPMS power state, brightness, and last user input as Home Assistant entities.

Home Assistant can:

- read whether the screen is on or off, and turn it on or off
- read and set brightness (0–100%)
- see the timestamp of the last keyboard/mouse/touchpad event (published once when the session goes idle, and immediately on the first input after that)

If the screen is off, any user input (mouse/keyboard) still wakes it through normal DPMS behaviour.

## How it works

| Action | Command |
| --- | --- |
| Read power | `kscreen-doctor --dpms show` |
| Turn off | `kscreen-doctor --dpms off` (standby; wakes on input) |
| Turn on | `kscreen-doctor --dpms on` |
| Read brightness | `kscreen-doctor -j` (`outputs[].brightness`, 0.0–1.0) |
| Set brightness | `kscreen-doctor output.<name>.brightness.<0-100>` |
| Last input | evdev `/dev/input/event*` (keyboard, mouse, touchpad) |

The bridge publishes retained power and brightness state and subscribes to command topics. It also publishes Home Assistant MQTT discovery payloads so the entities appear automatically.

Default topics (overridable in `config.json`):

Power switch:

- `homeassistant/switch/pc_monitor/config` — discovery
- `monitor/pc/state` — retained state (`ON` / `OFF`)
- `monitor/pc/set` — command (`ON` / `OFF`)

Brightness slider:

- `homeassistant/number/pc_monitor_brightness/config` — discovery
- `monitor/pc/brightness` — retained state (`0`–`100`)
- `monitor/pc/brightness/set` — command (`0`–`100`)

Inactivity:

- `homeassistant/sensor/pc_monitor_last_input/config` — discovery
- `monitor/pc/last_input` — retained UTC timestamp: once when input has been idle for 5 seconds (`idle_after`), and immediately again on the first input after that

The user running the bridge must be able to read `/dev/input/event*` (usually membership of the `input` group). Lid, speaker, HDMI-audio and similar devices are ignored.

If the process dies it publishes a last-will of `OFF` on the power state topic.

## Requirements

- Linux with KDE Plasma on Wayland
- `kscreen-doctor` (KScreen), with brightness control on the target output
- MQTT broker reachable from this machine
- Home Assistant with MQTT discovery enabled
- Python 3.11+

## Setup

```bash
git clone https://github.com/muvo243/wayland-mqtt-bridge.git ~/wayland-mqtt-bridge
cd ~/wayland-mqtt-bridge
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

Edit `config.json`:

- `mqtt.host` / `mqtt.port` / `mqtt.username` / `mqtt.password` — your broker
- `output` — KScreen output name (`eDP-1`, `HDMI-A-1`, …). Leave empty to use the first enabled output that supports brightness
- `switch.*` — name and identifiers shown in Home Assistant
- `topics.*` — MQTT topics if you run more than one machine
- `idle_after` — seconds without input before the last-input timestamp is published (default 5)

Do not commit `config.json`. It is gitignored.

Quick check without MQTT:

```bash
./run.sh state           # prints on / off
./run.sh off
./run.sh on
./run.sh brightness      # prints 0-100
./run.sh brightness 40
./run.sh idle            # sample ~2s: idle seconds + last-input UTC timestamp
```

Start the bridge:

```bash
./run.sh
```

## systemd user service

The unit file assumes the repo lives in `~/wayland-mqtt-bridge`. Adjust `WorkingDirectory` and `ExecStart` if you cloned elsewhere.

```bash
mkdir -p ~/.config/systemd/user
cp monitor-bridge.service ~/.config/systemd/user/
# edit the unit if the install path is not ~/wayland-mqtt-bridge
systemctl --user daemon-reload
systemctl --user enable --now monitor-bridge.service
systemctl --user status monitor-bridge.service
```

Keep the user session lingering so the service survives logout:

```bash
loginctl enable-linger "$USER"
```

## Home Assistant

With MQTT discovery enabled, these entities should appear after the bridge connects (same device):

- `switch.pc_monitor` — power
- `number.pc_monitor_brightness` — brightness slider (percent)
- `sensor.pc_monitor_last_input` — timestamp of last input (idle, then immediate resume)

Manual MQTT (if you skip discovery):

- power state: `monitor/pc/state` — `ON` / `OFF`
- power command: `monitor/pc/set` — `ON` / `OFF`
- brightness state: `monitor/pc/brightness` — `0`–`100`
- brightness command: `monitor/pc/brightness/set` — `0`–`100`
- last input: `monitor/pc/last_input` — UTC ISO-8601 timestamp (idle + first resume)

## Layout

```
config.example.json     # copy to config.json
monitor_bridge.py       # bridge
run.sh                  # finds Wayland/KDE env, then runs the bridge
monitor-bridge.service  # systemd --user unit template
requirements.txt
```
