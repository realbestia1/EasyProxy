# 🚀 EasyProxy

**Universal HLS/M3U8 Proxy & Stream Extractor**
A powerful, lightweight proxy server designed to handle HLS, M3U8, and DASH (MPD) streams. It includes specialized extractors for popular streaming services, DRM support, and an integrated DVR system.

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://python.org)
[![Docker](https://img.shields.io/badge/Docker-Ready-blue.svg)](https://docker.com)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## ✨ Features

- **🌐 Universal Proxy**: Seamlessly handles HLS, M3U8, MPD (DASH), and static video files.
- **🔓 DRM Support**: ClearKey decryption via legacy mode.
- **🔐 Specialized Extractors**: Native support for Vavoo, DaddyliveHD, Sportsonline, VixSrc, DoodStream, EmbedSports, and more.
- **📼 Integrated DVR**: Record live streams while watching or schedule background recordings.
- **🛠️ Playlist Builder**: Web interface to combine, manage, and proxy entire M3U playlists.
- **☁️ Cloud Ready**: Optimized for HuggingFace, Render, Koyeb, and other free-tier platforms.

---

## 🚀 Quick Start

### 🐳 Docker (Recommended)
The Docker image includes EasyProxy plus integrated CF Turnstile Solver for maximum compatibility. 

To run the container and persist config/recordings on your host machine, mount the `/data` directory:

```bash
docker run -d -p 7860:7860 -v ./data:/data --name EasyProxy ghcr.io/realbestia1/easyproxy:latest
```

### 🐍 Python (Local)

#### Prerequisites (All Platforms)
- **Python 3.11+**
- **Git** (for cloning dependencies)

#### 🪟 Windows Setup
The easiest way to get EasyProxy plus solvers on Windows:
1. Clone the repository and enter the folder.
2. Run **`start_full.bat`**.
*This script automatically handles CF Turnstile Solver, patches, and dependencies.*

#### 🐧 Linux / macOS Setup
1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
2. **Start EasyProxy**:
   ```bash
   python app.py
   ```
#### 📱 Termux (Android)
EasyProxy plus solvers is fully supported on Android via Termux + Ubuntu proot.

The setup supports proot-distro 5 and earlier releases, and installs Python dependencies in an isolated virtual environment at `/root/EasyProxy/.venv`.

For Termux, full functionality requires a 64-bit Android device. On 32-bit devices, some components and solvers may not work.

The setup also installs `wireproxy` (arm64/armv7) and the pinned WARP registration
script inside the Ubuntu guest, so Cloudflare WARP and the NordVPN/custom WireGuard
SOCKS5 tunnels work on Termux too. The first start registers WARP once and saves the
profile in `/data/warp.conf`; `enable_warp` in the Admin Panel only controls routing.

1.  **Install Termux** from [F-Droid](https://f-droid.org/en/packages/com.termux/) (do NOT use Play Store version).
2.  **Run the One-Shot Setup**:
    ```bash
    curl -fsSL --retry 3 "https://raw.githubusercontent.com/realbestia1/EasyProxy/main/termux_setup.sh?$(date +%s)" | bash
    ```
3.  **Prevent Termux from Sleeping**:
    - **Wake Lock**: Swipe down your notification bar and click **"Acquire wake-lock"** on the Termux notification.
    - **Battery Optimization**: Go to your Phone Settings -> Apps -> Termux -> Battery -> Set to **"Unrestricted"**.
4.  **Commands**:
    - `easyproxy`: Start the full stack.
    - `easyproxy-update`: Update code and dependencies.
    - `easyproxy-stop`: Stop all services.
    - `easyproxy-logs`: Follow the EasyProxy application log (`Ctrl+C` exits without stopping EasyProxy).
    - `easyproxy-logs --termux`: Follow the Termux/screen log.
    - `easyproxy-logs --attach`: Attach to the running screen session (`Ctrl+A`, then `D` to detach).
    - `easyproxy-logs --help`: Show all logging options.

If `easyproxy-update` reports `CANNOT LINK EXECUTABLE "curl"`, repair the
partially upgraded Termux packages first, then retry the update:

```bash
apt update && apt full-upgrade -y
easyproxy-update
```

*Access the dashboard at `http://localhost:7860`*

---

## 📦 Deployment Options

| Method | Description |
| :--- | :--- |
| **Docker** | Standard `docker build .` uses the single `Dockerfile` with solvers included. |
| **Docker Compose** | Run the complete stack (Proxy + Solvers) with `docker-compose up -d`. |
| **HuggingFace** | Use `Dockerfile-hf` for seamless deployment on HF Spaces. |
| **Termux** | Support for Android via Python. |

---

## ⚙️ Configuration

Most configuration settings (including Cloudflare WARP, DVR, and Proxy settings) are now managed directly from the **Admin Panel** at `http://localhost:7860/admin`.

Only basic environment variables need to be set in your `.env` file or container settings:

| Variable | Description | Default |
| :--- | :--- | :--- |
| `PORT` | Server port | `7860` |
| `API_PASSWORD` | Password to protect the proxy API and admin panel | `ep` |

### 🛡️ Cloudflare WARP Integration
The Docker image includes a pinned WARP registration script and `wireproxy`, providing a
userspace WireGuard SOCKS5 relay. The generated profile is saved in `/data/warp.conf`
and reused on subsequent starts.
It requires no `NET_ADMIN`, privileged mode, `/dev/net/tun`, kernel module, or
sysctl.

You can enable and configure WARP, customize the excluded domains list, and enter your license key directly from the **Admin Panel**.

### 🧭 NordVPN, custom WireGuard & TorProxy
Besides WARP, EasyProxy can run extra local SOCKS5 proxies:

| Panel | Profile source | Default SOCKS5 endpoint |
| :--- | :--- | :--- |
| `/admin/nordvpn` | NordLynx profile generated from your NordVPN access token and the server you pick | `socks5h://127.0.0.1:1081` |
| `/admin/wireguard` | Any WireGuard profile pasted into the panel | `socks5h://127.0.0.1:1082` |
| `/admin/torproxy` | Tor client managed by EasyProxy | `socks5h://127.0.0.1:9050` |

WARP keeps `127.0.0.1:1080`; each tunnel has its own process, port and log,
so they can run in parallel. Bind addresses are editable in their panels.

Tor is installed in the Docker image and starts only after enabling it from
`/admin/torproxy`. Automatic circuit rotation is disabled as far as Tor allows
(30-day maximum circuit lifetime); use **Request new IP** for manual `NEWNYM`.
An exit can still change after a failure or process restart. The panel includes
start/stop, manual identity change, Tor egress check and logs. **Exit country**
is a list loaded live from Tor (onionoo) with only the countries currently
running exit relays; picking one pins the relay Tor selects in that country, so
the egress IP stays fixed until **Request new IP**, which picks another relay
in the same country. Tor is TCP-only and should normally be used on selected
routes rather than as the default for all streaming traffic.

In the Admin Panel speed test every route - including **Direct** - is measured with
curl as a real TCP throughput test, shows the egress IP, and never falls back to
another route. Each route runs one 10-second download and one 10-second upload sample.

Reference the endpoint from **Global Proxies**, a **Transport Route** or an extractor
proxy to route EasyProxy traffic through it. TCP only: the SOCKS5 endpoint cannot
carry UDP. A missing `DNS`, `MTU` and `PersistentKeepalive` in a pasted profile is
filled with `1.1.1.1`, `1420` and `25`; IPv6 entries and wg-quick-only directives
(`Table`, `PostUp`, ...) are stripped before the tunnel starts.

### 🧩 VixSrc FlareSolverr
The Docker image also contains FlareSolverr, Chromium, and Xvfb. FlareSolverr is
not started at EasyProxy startup: VixSrc launches it only after detecting a
Cloudflare challenge, passes the currently selected proxy/WARP route, imports
the returned cookies and User-Agent, then terminates the process immediately.
When WARP is active, a missing solver route fails closed instead of using direct.

---

## 📖 API Usage
For detailed API documentation and testing, use the built-in **Interactive Docs** available at:
- `http://localhost:7860/docs` (Swagger UI)
- `http://localhost:7860/redoc` (ReDoc)

### 📺 Streaming Proxy
Prefix any stream URL with the proxy endpoint to handle headers and DRM.
```
http://localhost:7860/proxy/manifest.m3u8?url=<URL>
```
**Options:**
- `&clearkey=KID:KEY`: Provide keys for DASH streams.
- `&warp=off`: Force the request to bypass the WARP VPN and use the server's real IP (Direct Connection).
- `&proxy=off|torproxy|nordvpn|cwg`: Bypass all configured proxies (`off`) or force the whole stream through Tor (`torproxy`), NordVPN (`nordvpn`) or the custom WireGuard tunnel (`cwg`). Omit for automatic routing.
- `&host=<HOST>`: Force a specific extractor instead of auto-detection (e.g., `&host=vavoo`).
- `&max_res=true`: Serve only the highest video variant.
- `&h_<Header Name>=<Value>`: Pass custom headers (e.g., `&h_User-Agent=VLC`).

### 🔍 Stream Extractor
Extract direct video links from supported websites.
```
http://localhost:7860/extractor/video?d=<URL>&redirect_stream=true
```
*Tip: Open `http://localhost:7860/extractor` in your browser for a list of all parameters and supported hosts.*

### 📼 DVR & Recordings
Manage your recordings via the `/recordings` web UI or API.
- `/record?url=<URL>&name=<NAME>`: Start recording and watch simultaneously.
- Optional parameters: `extractor=<HOST>` (force a specific extractor instead of auto-detection), `max_res=1` (record only the highest video variant), `duration=<SECONDS>`, `key_id=<KID>&key=<KEY>` (ClearKey DRM), `proxy=off|torproxy|nordvpn|cwg` (bypass or force a proxy route).
- `/api/recordings/start`: Trigger a background recording (JSON body accepts the same options: `extractor`, `max_res`).

---

## 🛠️ Integrated Tools
- **Playlist Builder** (`/builder`): A visual tool to create custom M3U playlists with proxied links.
- **Server Info** (`/info`): Check status, public IP, and version information.

---

## 🤝 Contributing
Contributions are welcome!
1. **Fork** the repository.
2. **Commit** your changes (features, extractors, or bug fixes).
3. **Open a Pull Request** to the main branch.

*Found a bug? Open an [Issue](https://github.com/realbestia1/EasyProxy/issues)!*

---

## 📄 License
Distributed under the MIT License. See `LICENSE` for more information.

<div align="center">
  <p><b>⭐ If this project helped you, please give it a star! ⭐</b></p>
</div>
