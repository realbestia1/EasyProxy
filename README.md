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

### 🪟 Windows (Standalone Executable - Recommended on Windows)
On Windows **nothing needs to be installed** (no Python, no Docker, no external tools):
1. Download **`EasyProxy-windows-amd64.zip`** from the [Releases](https://github.com/realbestia1/EasyProxy/releases/latest) section.
2. Extract the ZIP into a folder of your choice.
3. Launch **`EasyProxy.exe`**.
4. Open the dashboard in your browser at **`http://localhost:7860`**.

> All required components are preconfigured and bundled: Wireproxy (WARP/WireGuard), Tor (Snowflake) and FlareSolverr (portable Chromium with automatic Turnstile solving).
>
> 🔄 **Built-in auto-update**: `EasyProxy.exe` updates itself with one click from the Web control panel (`/admin`) as soon as a new version is released on GitHub, or from a terminal with `EasyProxy.exe --update`.

### 🐳 Docker (Recommended)
The Docker image includes EasyProxy plus integrated CF Turnstile Solver for maximum compatibility. 

To run the container and persist config/recordings on your host machine, mount the `/data` directory:

```bash
docker run -d -p 7860:7860 -v ./data:/data --name EasyProxy ghcr.io/realbestia1/easyproxy:latest
```

### 📱 Termux (Android)
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
| **Windows Standalone** | Portable, self-contained executable (`EasyProxy.exe`) with Tor, WARP and FlareSolverr bundled. No prerequisites. |
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
| `API_PASSWORD` | Password to protect the proxy API and the API-facing pages (builder, URL generator, recordings) | `ep` |
| `ADMIN_PASSWORD` | Password for the admin panel and `/api/admin/*`. When set, it overrides `API_PASSWORD` for admin access | `API_PASSWORD` |

### 🛡️ Cloudflare WARP Integration
The Docker image includes a pinned WARP registration script and `wireproxy`, providing a
userspace WireGuard SOCKS5 relay. The generated profile is saved in `/data/warp.conf`
and reused on subsequent starts.
It requires no `NET_ADMIN`, privileged mode, `/dev/net/tun`, kernel module, or
sysctl.

You can enable and configure WARP, customize the excluded domains list, and enter your license key directly from the **Admin Panel**.

### 🧭 NordVPN, Custom WireGuard & TorProxy

EasyProxy supports multiple local SOCKS5 proxies running in parallel alongside Cloudflare WARP (`127.0.0.1:1080`). Each service runs its own isolated process, port, and log stream. Bind addresses can be customized directly in the panel.

| Panel | Profile Source | Default Endpoint |
| :--- | :--- | :--- |
| `/admin/nordvpn` | NordLynx profile generated via NordVPN access token | `socks5h://127.0.0.1:1081` |
| `/admin/wireguard` | Custom pasted WireGuard configuration | `socks5h://127.0.0.1:1082` |
| `/admin/torproxy` | Tor client managed by EasyProxy | `socks5h://127.0.0.1:9050` |

---

#### 🧅 Tor Features & Behavior
* **On-Demand:** Pre-installed; starts only when activated from `/admin/torproxy`.
* **Static Circuits:** Automatic rotation is suppressed up to Tor's 30-day limit. Click **Request new IP** for a manual `NEWNYM`.
* **Geo-Pinning:** Live country lists are fetched via Onionoo. Selecting an exit country pins the circuit to that country until manually refreshed.
* **Use Case:** TCP-only. Best suited for specific route targeting rather than primary streaming traffic.

#### ⚡ Route Speed Testing
Admin speed tests perform real TCP throughput measurements via `curl` across all routes (including **Direct**):
* Displays verified egress IP.
* Strictly avoids fallback to alternative routes.
* Runs dedicated 10-second download and 10-second upload tests per route.

#### ⚙️ WireGuard Profile Parsing
Pasted profiles are auto-sanitized before the interface is created:
* **Missing parameters filled:** `DNS` (`1.1.1.1`), `MTU` (`1420`), `PersistentKeepalive` (`25`).
* **Stripped elements:** IPv6 addresses and `wg-quick` directives (`Table`, `PostUp`, `PostDown`, etc.).
* **Limitation:** Endpoints are strictly TCP-only (UDP is not proxied).


### 🧩 FlareSolverr
The Docker image includes FlareSolverr, Chromium, and Xvfb, operating entirely on-demand to optimize resource usage:
* **On-Demand Execution:** Launched only when a Cloudflare challenge is detected, and terminates immediately after completion.
* **Route Integration:** Inherits the currently selected proxy or WARP route.
* **Session Handover:** Automatically captures and applies the resulting cookies and User-Agent.
* **Fail-Closed Security:** If WARP is active and the solver route is unavailable, requests fail closed rather than falling back to a direct connection.

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
