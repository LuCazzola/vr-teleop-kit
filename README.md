# vr-teleop-kit (Fork)

Compact Client-Server interface for VR-base Teleoperation.
* Forked from ([https://github.com/Dream-Machines-Robotics/vr-teleop-kit](https://github.com/Dream-Machines-Robotics/vr-teleop-kit))
* Kept only Web-side elements + added basic client

> That fits all purposes! you may for instance plug it in your ROS2 project or whatever 


## Architecture

2 layers, separated by how robot-specific they are:

```
src/vr_teleop_kit/
├── relay/     robot-agnostic: FastAPI WebSocket relay, WebRTC cameras,
│              and the WebXR page served to the Quest
├── tools/     Data logging web apps
```

## Install

```bash
git clone https://github.com/LuCazzola/vr-teleop-kit
cd vr-teleop-kit
pip install -e ".[relay]"           # or: uv pip install -e ".[relay]"
```

The `relay` extra covers the server (FastAPI, aiortc, OpenCV);

## Run the relay and connect the headset

Both transports serve the same page on port 8443; they differ only in
how the Quest reaches it. WebXR requires a secure context, which is why
the two paths exist.

**USB (recommended)**: plain HTTP on localhost (a secure context per the
WebXR spec), forwarded over the cable:

```bash
vr-teleop-relay                               # binds 127.0.0.1:8443
adb reverse tcp:8443 tcp:8443                 # forward Quest's localhost over USB
# Quest browser → http://localhost:8443/
```
> NOTE: you may run `adb devices` to check if your PC actually sees the VR headset

One-time Quest setup: enable Developer Mode (Meta Quest mobile app →
Devices → your headset → Developer Mode), plug in the cable, accept the
"Allow USB debugging" prompt in the headset. `adb reverse` must be re-run
after re-plugging the cable.

**LAN**: HTTPS with a self-signed certificate; the Quest shows a
"not secure" warning the first time, which you accept:

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:4096 -nodes -days 825 \
    -keyout certs/key.pem -out certs/cert.pem \
    -subj "/CN=$(hostname)" \
    -addext "subjectAltName=DNS:$(hostname),DNS:localhost,IP:127.0.0.1,IP:<your-lan-ip>"
vr-teleop-relay --host 0.0.0.0 --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem
# Quest browser → https://<your-lan-ip>:8443/
```

`certs/` is gitignored; never commit key material. USB has ~1 ms RTT and
no jitter; LAN works but Wi-Fi adds occasional >100 ms spikes. A public
tunnel (e.g. `cloudflared tunnel --url http://localhost:8443`) also works
for pose streaming, but WebRTC camera media is peer-to-peer and needs a
TURN server off-LAN (not configured here).

On the page: **Calibrate wrist** once per operator (squeeze both grips
in VR, then for 5 s twist your hands while keeping each wrist roughly in
place: the hand rotates, the wrist pivot stays still), then
**Start Teleop**. Settings (gains, smoothing, velocity caps, haptics)
are on the same page and apply live.

## License

Apache-2.0.
