# screenbridge

MQTT bridge that exposes a KDE/Wayland display's DPMS power state as a Home Assistant switch.

Home Assistant can read whether the screen is on or off, and turn it on or off over MQTT. If the screen is off, any user input (mouse/keyboard) still wakes it through normal DPMS behaviour.

## How it works

| Action | Command |
| --- | --- |
| Read state | `kscreen-doctor --dpms show` |
| Turn off | `kscreen-doctor --dpms off` (standby; wakes on input) |
| Turn on | `kscreen-doctor --dpms on` |

The bridge publishes retained `ON` / `OFF` state and subscribes to a command topic. It also publishes a Home Assistant MQTT discovery payload so the switch entity appears automatically.

Default topics (overridable in `config.json`):

- `homeassistant/switch/pc_monitor/config` — discovery
- `monitor/pc/state` — retained state (`ON` / `OFF`)
- `monitor/pc/set` — command (`ON` / `OFF`)

If the process dies it publishes a last-will of `OFF`.

## Requirements

- Linux with KDE Plasma on Wayland
- `kscreen-doctor` (KScreen)
- MQTT broker reachable from this machine
- Home Assistant with MQTT discovery enabled
- Python 3.11+

## Setup

```bash
git clone <this-repo> ~/screenbridge
cd ~/screenbridge
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

Edit `config.json`:

- `mqtt.host` / `mqtt.port` / `mqtt.username` / `mqtt.password` — your broker
- `switch.*` — name and identifiers shown in Home Assistant
- `topics.*` — MQTT topics if you run more than one machine

Do not commit `config.json`. It is gitignored.

Quick check without MQTT:

```bash
./run.sh state    # prints on / off
./run.sh off
./run.sh on
```

Start the bridge:

```bash
./run.sh
```

## systemd user service

The unit file assumes the repo lives in `~/screenbridge`. Adjust `WorkingDirectory` and `ExecStart` if you cloned elsewhere.

```bash
mkdir -p ~/.config/systemd/user
cp monitor-bridge.service ~/.config/systemd/user/
# edit the unit if the install path is not ~/screenbridge
systemctl --user daemon-reload
systemctl --user enable --now monitor-bridge.service
systemctl --user status monitor-bridge.service
```

Keep the user session lingering so the service survives logout:

```bash
loginctl enable-linger "$USER"
```

## Home Assistant

With MQTT discovery enabled, a switch entity (default `switch.pc_monitor`) should appear after the bridge connects. You can then use it in dashboards and automations.

Manual MQTT (if you skip discovery):

- state topic: `monitor/pc/state`
- command topic: `monitor/pc/set`
- payloads: `ON` / `OFF`

## Layout

```
config.example.json     # copy to config.json
monitor_bridge.py       # bridge
run.sh                  # finds Wayland/KDE env, then runs the bridge
monitor-bridge.service  # systemd --user unit template
requirements.txt
```
