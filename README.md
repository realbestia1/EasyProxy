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
   python -m playwright install chromium
   ```
2. **Start EasyProxy**:
   ```bash
   python app.py
   ```
#### 📱 Termux (Android)
EasyProxy plus solvers is fully supported on Android via Termux + Ubuntu proot.

Android users can also install the APK build if they prefer a simpler app-style setup. The APK is convenient, but it is not as complete as the Python/Termux version, so Termux remains the recommended option for full functionality.

For Termux, full functionality requires a 64-bit Android device. On 32-bit devices, some components and solvers may not work.

1.  **Install Termux** from [F-Droid](https://f-droid.org/en/packages/com.termux/) (do NOT use Play Store version).
2.  **Run the One-Shot Setup**:
    ```bash
    curl -sL "https://raw.githubusercontent.com/realbestia1/EasyProxy/main/termux_setup.sh?$(date +%s)" | bash
    ```
3.  **Prevent Termux from Sleeping**:
    - **Wake Lock**: Swipe down your notification bar and click **"Acquire wake-lock"** on the Termux notification.
    - **Battery Optimization**: Go to your Phone Settings -> Apps -> Termux -> Battery -> Set to **"Unrestricted"**.
4.  **Commands**:
    - `easyproxy`: Start the full stack.
    - `easyproxy-update`: Update code and dependencies.
    - `easyproxy-stop`: Stop all services.

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
| `WIDEVINE_WVD_PATH` | Private Widevine device file used for personal Mediaset/WittyTV/RaiPlay sessions | `/data/cdm/widevine-device.wvd` |

### 🛡️ Cloudflare WARP Integration
The Docker image includes an integrated Cloudflare WARP client to bypass IP-based blocks.

You can enable and configure WARP, customize the excluded domains list, and enter your license key directly from the **Admin Panel**.

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
- `&h_<Header Name>=<Value>`: Pass custom headers (e.g., `&h_User-Agent=VLC`).

### 🔍 Stream Extractor
Extract direct video links from supported websites.
```
http://localhost:7860/extractor/video?d=<URL>&redirect_stream=true
```
*Tip: Open `http://localhost:7860/extractor` in your browser for a list of all parameters and supported hosts.*

### Mediaset Infinity, WittyTV and RaiPlay playback sessions

EasyProxy can resolve a Mediaset Infinity or WittyTV VOD, keep its temporary playback material only
in server memory, and expose an opaque DASH URL. Configure a private Widevine
device file with `WIDEVINE_WVD_PATH` (`MEDIASET_WVD_PATH` and the legacy `WITTY_WVD_PATH` are also
accepted), or mount it at `/data/cdm/widevine-device.wvd`, and
set a non-empty `API_PASSWORD`, then create sessions server-to-server:

With the included Compose volume, the device file at
`./data/cdm/widevine-device.wvd` appears inside the container as
`/data/cdm/widevine-device.wvd`.

```http
POST /mediaset/session
X-API-Password: your-password
Content-Type: application/json

{"pageUrl":"https://mediasetinfinity.mediaset.it/movie/example/example_F000000000000000","guid":"F000000000000000"}
```

The response URL contains only a random, expiring playback token. API passwords,
Mediaset bearer tokens and content keys are never embedded in the manifest or
segment URLs. This feature is intended for personal instances and content the
user is authorized to access. Keep `WORKERS=1`, because sessions are memory-only.

RaiPlay clear HLS streams should remain direct. For Widevine DASH, EasyProxy
resolves the short-lived Rai relinker URL, obtains the authorized content keys
and refreshes expired CDN tokens while the opaque session is active:

```http
POST /raiplay/session
X-API-Password: your-password
Content-Type: application/json

{"contentId":"RaiPlay-relinker-content-id","title":"Example"}
```

### 📼 DVR & Recordings
Manage your recordings via the `/recordings` web UI or API.
- `/record?url=<URL>&name=<NAME>`: Start recording and watch simultaneously.
- `/api/recordings/start`: Trigger a background recording.

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
