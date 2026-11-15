# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# Explicit discovery of all local modules
hidden_imports = []
for folder in ['extractors', 'services', 'routes', 'utils', 'scripts']:
    for p in Path(folder).glob('*.py'):
        if p.stem != '__init__':
            hidden_imports.append(f'{folder}.{p.stem}')

hidden_imports.extend([
    'services.dual',
    'services.dual.service',
    'aiohttp',
    'aiohttp_socks',
    'aiofiles',
    'aiosqlite',
    'cryptography',
    'curl_cffi',
    'curl_cffi.requests',
    'selectolax',
    'bs4',
    'lxml',
    'Crypto',
    'Crypto.Cipher.AES',
    'Crypto.Util.Padding',
    'nacl',
    'PIL',
    'psutil',
    'pywidevine',
    'python_socks',
    'dotenv',
    'engineio.async_drivers.aiohttp',
])

# Bundled data files
datas = [
    ('templates', 'templates'),
    ('static', 'static'),
    ('scripts', 'scripts'),
    ('extractors/widevine-device.wvd', 'extractors'),
    ('extractors/extractor_test_urls.json', 'extractors'),
]

if Path('bin').is_dir():
    datas.append(('bin', 'bin'))
if Path('data/warp_wireproxy.conf').is_file():
    datas.append(('data/warp_wireproxy.conf', 'data'))
if Path('data/geoip').is_file():
    datas.append(('data/geoip', 'data'))
if Path('data/geoip6').is_file():
    datas.append(('data/geoip6', 'data'))
# Node deps for scripts/*.mjs runners (happy-dom, used by ppv_runner.mjs).
# Lands in _MEIPASS/scripts/node_modules, where the runner resolves it from.
if Path('node_modules').is_dir():
    datas.append(('node_modules', 'scripts/node_modules'))

# Collect curl_cffi and pywidevine data files if present
datas += collect_data_files('curl_cffi')
datas += collect_data_files('pywidevine')

a = Analysis(
    ['app.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'torch', 'torchvision', 'transformers', 'behave'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='EasyProxy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='static/favicon.ico',
)
