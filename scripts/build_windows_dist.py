"""Build and package EasyProxy standalone Windows distribution."""
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT_DIR / "bin"
DATA_DIR = ROOT_DIR / "data"
DIST_DIR = ROOT_DIR / "dist"
FLARESOLVERR_DIR = ROOT_DIR / "flaresolverr"

WIREPROXY_URL = "https://github.com/windtf/wireproxy/releases/download/v1.1.3/wireproxy_windows_amd64.tar.gz"
TOR_BUNDLE_URL = "https://archive.torproject.org/tor-package-archive/torbrowser/14.0.9/tor-expert-bundle-windows-x86_64-14.0.9.tar.gz"
FLARESOLVERR_RELEASE_URL = "https://github.com/FlareSolverr/FlareSolverr/releases/download/v3.5.2/flaresolverr_windows_x64.zip"


def download_file(url: str, dest: Path) -> None:
    print(f"Downloading {url} -> {dest.name}...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)


def prepare_binaries() -> None:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    wireproxy_exe = BIN_DIR / "wireproxy.exe"
    if not wireproxy_exe.is_file():
        archive = ROOT_DIR / "temp_wireproxy.tar.gz"
        download_file(WIREPROXY_URL, archive)
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if Path(member.name).name.lower() in ("wireproxy.exe", "wireproxy"):
                    member.name = "wireproxy.exe"
                    tar.extract(member, path=BIN_DIR)
        archive.unlink(missing_ok=True)
        print("Extracted wireproxy.exe")

    # tor MUST NOT be installed as "tor.exe": a Windows security component
    # blocks TLS traffic of processes literally named "tor.exe" (handshakes
    # abort on every relay), while the identical binary bootstraps fine under
    # any other name. It therefore ships as "torclient.exe".
    tor_exe = BIN_DIR / "torclient.exe"
    legacy_tor_exe = BIN_DIR / "tor.exe"
    lyrebird_exe = BIN_DIR / "lyrebird.exe"
    if legacy_tor_exe.is_file():
        legacy_tor_exe.rename(tor_exe)
        print("Renamed bin/tor.exe -> bin/torclient.exe (tor.exe is blocked by Windows)")
    if not tor_exe.is_file() or not lyrebird_exe.is_file():
        archive = ROOT_DIR / "temp_tor.tar.gz"
        download_file(TOR_BUNDLE_URL, archive)
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                name_lower = Path(member.name).name.lower()
                if name_lower == "tor.exe":
                    member.name = "torclient.exe"
                    tar.extract(member, path=BIN_DIR)
                elif name_lower == "lyrebird.exe":
                    member.name = "lyrebird.exe"
                    tar.extract(member, path=BIN_DIR)
                elif name_lower == "geoip":
                    member.name = "geoip"
                    tar.extract(member, path=DATA_DIR)
                elif name_lower == "geoip6":
                    member.name = "geoip6"
                    tar.extract(member, path=DATA_DIR)
        archive.unlink(missing_ok=True)
        print("Extracted torclient.exe, lyrebird.exe, geoip")


def prepare_flaresolverr() -> None:
    flare_exe = FLARESOLVERR_DIR / "flaresolverr.exe"
    flare_chrome = FLARESOLVERR_DIR / "_internal" / "chrome" / "chrome.exe"
    if flare_exe.is_file() and flare_chrome.is_file():
        print("FlareSolverr already prepared.")
        return

    # 1. Download official release for bundled Chromium
    flare_zip = ROOT_DIR / "temp_flaresolverr.zip"
    flare_extract = ROOT_DIR / "temp_flare_extract"
    download_file(FLARESOLVERR_RELEASE_URL, flare_zip)
    print("Extracting FlareSolverr release...")
    with zipfile.ZipFile(flare_zip, "r") as z:
        z.extractall(flare_extract)
    flare_zip.unlink(missing_ok=True)

    extracted_flare = flare_extract / "flaresolverr"
    if not extracted_flare.exists():
        extracted_flare = flare_extract

    # 2. Clone FlareSolverr v3.5.2 source and apply Turnstile patch
    src_dir = ROOT_DIR / "temp_flare_src"
    if src_dir.exists():
        shutil.rmtree(src_dir)
    print("Cloning FlareSolverr v3.5.2 source...")
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", "v3.5.2",
         "https://github.com/FlareSolverr/FlareSolverr.git", str(src_dir)],
        check=True
    )

    sys.path.insert(0, str(ROOT_DIR))
    from services.flaresolverr import patch_turnstile_navigation
    patch_turnstile_navigation(str(src_dir / "src" / "flaresolverr.py"))

    # 3. Build patched flaresolverr.exe using PyInstaller
    build_dir = ROOT_DIR / "temp_flare_build"
    dist_temp = ROOT_DIR / "temp_flare_dist"
    print("Compiling patched flaresolverr.exe...")
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--onedir",
            "--name", "flaresolverr",
            str(src_dir / "src" / "flaresolverr.py"),
            "--distpath", str(dist_temp),
            "--workpath", str(build_dir),
            "-y"
        ],
        check=True
    )

    # 4. Assemble final flaresolverr directory
    FLARESOLVERR_DIR.mkdir(parents=True, exist_ok=True)
    built_flare = dist_temp / "flaresolverr"
    shutil.copy2(built_flare / "flaresolverr.exe", FLARESOLVERR_DIR / "flaresolverr.exe")
    
    internal_dest = FLARESOLVERR_DIR / "_internal"
    if internal_dest.exists():
        shutil.rmtree(internal_dest)
    shutil.copytree(built_flare / "_internal", internal_dest)

    # Copy chrome and package.json from extracted official release
    src_chrome = extracted_flare / "_internal" / "chrome"
    if not src_chrome.exists():
        src_chrome = extracted_flare / "chrome"
    shutil.copytree(src_chrome, internal_dest / "chrome")

    src_pkg = extracted_flare / "_internal" / "package.json"
    if not src_pkg.exists():
        src_pkg = extracted_flare / "package.json"
    if src_pkg.exists():
        shutil.copy2(src_pkg, internal_dest / "package.json")

    # Clean up temporary build files
    shutil.rmtree(flare_extract, ignore_errors=True)
    shutil.rmtree(src_dir, ignore_errors=True)
    shutil.rmtree(build_dir, ignore_errors=True)
    shutil.rmtree(dist_temp, ignore_errors=True)
    print("Patched FlareSolverr ready.")


def ensure_node_modules() -> None:
    """PPV/NTV runners import happy-dom; easyproxy.spec bundles node_modules."""
    node = shutil.which("node") or next(
        (str(p) for p in (BIN_DIR / "node.exe", ROOT_DIR / "node.exe") if p.is_file()), None
    )
    if not node:
        print("WARNING: node not found. Install Node.js or place node.exe in bin/, "
              "otherwise PPV/NTV extraction will fail in the exe.")

    marker = ROOT_DIR / "node_modules" / "happy-dom" / "package.json"
    if marker.exists():
        print("happy-dom already installed.")
        return
    npm = shutil.which("npm")
    if not npm:
        print("WARNING: npm not found. Install Node.js and run "
              "'npm install --no-save happy-dom@20.10.6' before building, "
              "otherwise PPV/NTV extraction will fail in the exe.")
        return
    print("Installing happy-dom for the PPV/NTV runners...")
    subprocess.run([npm, "install", "--no-save", "happy-dom@20.10.6"],
                   cwd=ROOT_DIR, check=True)


def build_easyproxy_exe() -> None:
    ensure_node_modules()
    print("Building EasyProxy.exe with PyInstaller...")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "easyproxy.spec", "--clean", "-y"],
        cwd=ROOT_DIR,
        check=True
    )


def assemble_distribution() -> Path:
    print("Assembling final distribution...")
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    # Ensure bin/ is copied to dist/bin/
    dist_bin = DIST_DIR / "bin"
    dist_bin.mkdir(parents=True, exist_ok=True)
    for exe in BIN_DIR.glob("*.exe"):
        shutil.copy2(exe, dist_bin / exe.name)

    # Ensure data/ is copied to dist/data/
    dist_data = DIST_DIR / "data"
    dist_data.mkdir(parents=True, exist_ok=True)
    for item in DATA_DIR.glob("*"):
        if item.is_file():
            shutil.copy2(item, dist_data / item.name)

    # Ensure flaresolverr/ is copied to dist/flaresolverr/
    dist_flare = DIST_DIR / "flaresolverr"
    if not (dist_flare / "flaresolverr.exe").is_file():
        if dist_flare.exists():
            shutil.rmtree(dist_flare)
        shutil.copytree(FLARESOLVERR_DIR, dist_flare)

    # Create zip archive in ROOT_DIR / "release_assets"
    release_dir = ROOT_DIR / "release_assets"
    release_dir.mkdir(parents=True, exist_ok=True)
    zip_path = release_dir / "EasyProxy-windows-amd64.zip"
    print(f"Creating zip archive: {zip_path.name}...")
    if zip_path.exists():
        zip_path.unlink()

    base_name = str(release_dir / "EasyProxy-windows-amd64")
    shutil.make_archive(base_name, "zip", root_dir=DIST_DIR)
    print(f"Distribution archive ready: {zip_path} ({zip_path.stat().st_size} bytes)")
    return zip_path


def main() -> None:
    prepare_binaries()
    prepare_flaresolverr()
    build_easyproxy_exe()
    zip_path = assemble_distribution()
    print(f"\nAll done! Standalone release package: {zip_path}")


if __name__ == "__main__":
    main()
