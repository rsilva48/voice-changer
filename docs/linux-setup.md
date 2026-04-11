# Voice Changer — Linux Setup Guide

Tested on Arch Linux (CachyOS) with PipeWire 1.6+. Most steps apply to any distro
running PipeWire. ALSA-only setups may need minor adjustments.

---

## 1. System dependencies

### Arch / Manjaro / CachyOS

```bash
sudo pacman -S python python-pip portaudio alsa-utils pipewire pipewire-pulse wireplumber
```

> **Note**: `portaudio` is required by `sounddevice`. If it is missing, `pip install sounddevice` will fail at runtime.

### Ubuntu / Debian

```bash
sudo apt install python3 python3-pip python3-venv portaudio19-dev alsa-utils
```

---

## 2. Installation

```bash
cd server
./vc_install.sh
```

Select backend:
- `1` — CPU (any machine)
- `2` — CUDA (NVIDIA)
- `3` — DirectML (Windows only)
- `4` — ROCm (AMD Linux)

---

## 3. AMD ROCm setup

### 3.1 Install ROCm

Follow the official guide for your distro:
<https://rocm.docs.amd.com/projects/install-on-linux/en/latest/>

On Arch: `paru -S rocm-hip-sdk rocm-opencl-sdk`

### 3.2 Add user to render/video groups

```bash
sudo usermod -aG render,video $USER
# Log out and back in, or:
newgrp render
```

### 3.3 Verify GPU is detected

```bash
rocminfo | grep -E "Name:|gfx"
```

### 3.4 gfx version override (unsupported GPUs)

If your GPU is not officially listed (e.g. gfx1201 / RDNA4) you need to override:

```bash
export HSA_OVERRIDE_GFX_VERSION=12.0.0   # adjust to your GPU
```

Add it to `vc_startup.sh` inside `start_app()`.

### 3.5 PyTorch `.so` library patch (if needed)

Some ROCm/PyTorch versions ship `.so` files with names that don't match what the
linker expects. If you see errors like `libamdhip64.so.6: cannot open shared object`,
create symlinks:

```bash
# Example — find the actual versioned file first:
find ~/.../venv/lib -name "libamdhip64*.so*" 2>/dev/null

# Then symlink without version suffix:
ln -sf libamdhip64.so.6.X.Y libamdhip64.so.6
```

Alternatively, set `LD_LIBRARY_PATH` to the ROCm lib dir before starting.

### 3.6 Suppress log spam

ROCm prints large amounts of HIP/MIOpen debug output by default:

```bash
export AMD_LOG_LEVEL=0
export MIOPEN_LOG_LEVEL=2   # 2 = only errors; 0 = silent
```

These are already set in `vc_startup.sh`.

---

## 4. Audio on Linux — choosing devices

Linux exposes audio through several layers. The server uses **sounddevice** (PortAudio)
which talks to ALSA. When PipeWire is present it intercepts ALSA.

### 4.1 List available devices

```bash
cd server
source venv/bin/activate
python list_devices.py
```

Or with plain Python:

```python
import sounddevice as sd
for i, d in enumerate(sd.query_devices()):
    print(i, d['name'])
```

### 4.2 Safe devices under PipeWire

| Device name | Role | Notes |
|-------------|------|-------|
| `pipewire`  | **Input** | Captures from PipeWire default source. Same ALSA plugin clock regardless of output device. |
| `pulse`     | **Output / Monitor** | Routes via PulseAudio compat layer. Respects `PULSE_SINK` env var to send audio to any named sink (e.g. snd-aloop loopback). |
| `default`   | ⚠️ avoid | Unstable under PipeWire, may map to a `hw:` device directly. |
| `hw:X,Y`    | ❌ never | Direct ALSA hardware access — PipeWire holds exclusive ownership, causes **SIGABRT**. |
| `plughw:X,Y`| ❌ never | Same as `hw:`. |

> The server (`ServerAudio._is_safe_device()`) refuses to open `hw:` and `plughw:` devices and logs a clear error message instead of crashing.

### 4.3 Recommended configuration

| Device | Setting | Why |
|--------|---------|-----|
| Input | `pipewire` | Stable capture clock, routes to PipeWire default source (your mic) |
| Output | `pulse` | Separate stream from input — avoids PortAudio duplex clock-sync crash — routed to snd-aloop via `PULSE_SINK` |
| Monitor | *(disabled, -1)* | Not needed; Discord reads from RVC-Microphone source directly |

**Why input and output must be different devices**: when `pipewire` is used for both
in a single `sd.Stream` (duplex), PortAudio tries to synchronise the clocks of the
input and output paths. Under PipeWire this synchronisation fails intermittently,
causing ALSA underruns and eventual crash (`pa_linux_alsa.c` assertion). Using
`pipewire` for input and `pulse` for output opens two independent streams
(`sd.InputStream` + `sd.OutputStream`) — each with its own clock — so they never
conflict.

**Device name persistence**: device indices in ALSA change every time a new virtual
source is created (e.g. RVC-Mic on startup). The server resolves devices by **name**
at startup using `serverInputDeviceName` / `serverOutputDeviceName` from
`stored_setting.json`, so the numeric ID in that file is only a fallback and does not
need to be kept up to date. IDs are resolved both in `VoiceChangerManager.__init__`
(so the UI shows the right devices immediately) and again in `ServerAudio.start()`
(so the stream opens with the current indices).

**Channel count**: ALSA virtual devices like `pipewire` report up to 128 input
channels. Opening a stream with that many channels causes PipeWire to use non-standard
routing, resulting in glitchy, overlapping audio. The server caps all ALSA streams to
**2 channels (stereo)** automatically.

### 4.4 Routing input to a specific microphone

PipeWire reads `PIPEWIRE_ALSA` before opening the ALSA plugin. You can pin capture
to a specific node:

```bash
export PIPEWIRE_ALSA='{"alsa.rate": 48000, "capture.props": {"target.object": "alsa_input.usb-YOUR_MIC_ID-00.pro-input-0"}}'
```

Find the node name:

```bash
pw-cli ls Node | grep -i "your mic name"
```

### 4.5 Low microphone volume

USB microphones often have a conservative hardware ADC gain. If the signal is too
quiet even at 100% input gain in the UI, boost the capture volume at the PipeWire
level. WirePlumber persists the value automatically:

```bash
# Find your source ID:
wpctl status | grep -A10 "Sources:"

# Set volume (1.5 = 150%):
wpctl set-volume <SOURCE_ID> 1.5
```

The value is saved to `~/.local/state/wireplumber/default-routes` and restored on
every reconnect.

### 4.6 Sample rate

Set all four sample rate fields to the same value:

| Setting | Recommended |
|---------|-------------|
| `serverInputAudioSampleRate` | 48000 |
| `serverOutputAudioSampleRate` | 48000 |
| `serverMonitorAudioSampleRate` | 48000 |
| `serverAudioSampleRate` | 48000 |

---

## 5. Virtual Audio Cable (VAC) — routing to Discord / OBS

To use the RVC output as microphone input in other apps:

### 5.1 Load snd-aloop (kernel loopback module)

```bash
sudo modprobe snd-aloop
```

Make it permanent (survives reboot):

```bash
echo "snd-aloop" | sudo tee /etc/modules-load.d/snd-aloop.conf
```

### 5.2 How it works

`snd-aloop` creates a kernel-level soundcard with two subdevices that are wired together:
- Write to **Loopback PCM (hw:X,0)** → readable on **hw:X,1**
- PipeWire exposes both as:
  - `alsa_output.platform-snd_aloop.0.analog-stereo` (sink — write here)
  - `alsa_output.platform-snd_aloop.0.analog-stereo.monitor` (PipeWire virtual monitor — read here in Discord)

### 5.3 Route output to the loopback

In `vc_startup.sh`:

```bash
export PULSE_SINK=alsa_output.platform-snd_aloop.0.analog-stereo
```

Set `serverOutputDeviceId` to the `pulse` device index (typically `13` on Arch with
PipeWire, but check with `list_devices.py` after loading snd-aloop).

### 5.4 Select in Discord / OBS

In Discord → Settings → Voice & Video → Input Device:

- Select **"Monitor of Loopback Analog Stereo"**
  (NOT "Loopback Analog Stereo" — that is the raw ALSA capture side and may be silent)

In OBS → Audio → Mic/Aux:

- Same source: **"Monitor of Loopback Analog Stereo"**

### 5.5 Verify audio is flowing

```bash
pactl list sources short | grep -i loop
# Both should show "RUNNING" when the server is active:
# alsa_output.platform-snd_aloop.0.analog-stereo.monitor
# alsa_input.platform-snd_aloop.0.analog-stereo
```

---

## 6. Building the frontend

The web UI is built separately and served as static files by the server.

### Dependencies

```bash
# Node 18+ required
node --version
npm --version
```

### Build

```bash
cd client/modern-gui
npm install
npm run build:prod
```

Output goes to `client/modern-gui/dist/` and is automatically served by the server
at the root URL.

### Development build (hot reload)

```bash
npm run build:dev
```

> **Note**: The prebuilt `dist/` is committed to the repo. If you only modified Python
> code you do not need to rebuild.

---

## 7. Starting the server

```bash
cd server
./vc_startup.sh
```

Open `http://localhost:18888` in your browser.

> ⚠️ The Python client launcher (`client.py`) does not currently auto-open the browser
> correctly on all Linux desktops. Use the URL directly instead.

---

## 8. Known issues and fixes applied

### 8.1 SIGABRT on startup (hw: device under PipeWire)

**Symptom**: Server crashes with `Aborted (core dumped)` immediately on start.

**Cause**: PortAudio tries to open a `hw:X,Y` device directly while PipeWire holds
exclusive access.

**Fix**: Use `pipewire` or `pulse` as device name, never `hw:` or `plughw:`. The server
now checks this in `ServerAudio._is_safe_device()` and returns a clear error message
instead of crashing.

Do not call `sd._terminate()` / `sd._initialize()` — PortAudio re-queries ALSA and
device indices change, causing wrong devices to be opened.

---

### 8.2 Monitor stream freeze (deadlock)

**Symptom**: Audio stops, server freezes when changing the monitor device.

**Cause**: `audio_monitor_callback` was calling `monQueue.get(block=True)` (blocking
forever) inside a PortAudio callback. If the queue was empty the callback thread
deadlocked.

**Fix** (applied in `ServerAudio`):
- Use `monQueue.get(block=True, timeout=0.05)` — fill output with silence on timeout.
- `stop()` drains the queue before closing streams.

---

### 8.3 ServerIO Analyzer returns HTTP 416 (WAV file empty)

**Symptom**: Clicking "Analyze" in the ServerIO panel returns 416 Range Not Satisfiable.

**Cause**: The WAV file header was written with `nframes=0` because `IORecorder` was never
closed when recording was stopped.

**Fix** (applied in `VoiceChangerV2.update_settings()`):
```python
elif key == 'recordIO':
    if int(val) == 0:
        self.io_recorder.close()   # flushes nframes into WAV header
    elif int(val) == 1:
        self.io_recorder.open(...)
```

---

### 8.4 NoneType error on device lookup

**Symptom**: `AttributeError: 'NoneType' object has no attribute 'maxInputChannels'`

**Cause**: A second call to `list_audio_device()` inside `getServerInputAudioDevice()`
returned `None` if ALSA hadn't settled (especially after `sd._initialize()`).

**Fix**: The device is now looked up directly from the dict built by the first call, no
second query needed.

---

### 8.5 audioEffects reverts to `{}` on server save

**Symptom**: `[AudioEffectsCard] audioEffects should be a list, got <class 'dict'>` in
the browser console after restarting the server.

**Cause**: On shutdown, the server serialises `VoiceChangerSettings` to JSON. If
`audioEffects` is an empty list `[]`, some serialisers write it back as `{}`.

**Workaround**: The frontend (`AudioEffectsCard.tsx`) now guards with `Array.isArray`
before rendering, so it doesn't crash. A proper server-side fix would normalise `{}`
→ `[]` on load in `VoiceChangerSettings`.

---

### 8.6 "Client audio not available" warning in server mode

**Symptom**: A warning banner appears even when server audio is enabled and working.

**Fix**: `AudioMode.tsx` suppresses the warning when `enableServerAudio === 1`.

---

### 8.7 Model upload modal crash (embedders)

**Symptom**: Uploading a model crashes the UI with `Cannot read properties of undefined`.

**Fix**: `UploadModelModal.tsx` now uses `Object.values(embedders || {})[0]` as fallback
instead of `embedders[0]`.

---

## 9. Environment variables reference

| Variable | Value | Purpose |
|----------|-------|---------|
| `AMD_LOG_LEVEL` | `0` | Silence AMD HIP log spam |
| `MIOPEN_LOG_LEVEL` | `2` | MIOpen errors only |
| `HSA_OVERRIDE_GFX_VERSION` | `12.0.0` | Override GPU arch for unsupported GPUs |
| `PULSE_SINK` | sink name | Route `pulse` device output to specific PipeWire sink |
| `PIPEWIRE_ALSA` | JSON string | Configure PipeWire ALSA plugin (rate, format, device routing) |

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| SIGABRT on start | `hw:` device selected | Use `pipewire` or `pulse` |
| No audio in Discord | Wrong loopback source | Use "Monitor of Loopback Analog Stereo" |
| Constant ALSA underruns | Sample rate mismatch | Set all sample rates to 48000 |
| Server freezes on device change | Monitor queue deadlock | Fixed — update code |
| WAV download 416 error | nframes=0 in WAV header | Fixed — update code |
| HIP fatbin spam in logs | AMD_LOG_LEVEL=2 | Set AMD_LOG_LEVEL=0 |
| Device indices change on restart | `sd._terminate()` called | Do not call it; fixed in code |
