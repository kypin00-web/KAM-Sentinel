# scripts/gpu_bench.spec
# PyInstaller spec for KAM_GPU_Bench.exe
#
# Build from project root:
#   python -m PyInstaller scripts/gpu_bench.spec
#
# Output: dist/KAM_GPU_Bench.exe

import os
block_cipher = None

a = Analysis(
    [os.path.join('scripts', 'gpu_bench.py')],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'moderngl',
        'moderngl.mgl',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'pandas'],
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
    name='KAM_GPU_Bench',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # console=True: shows output when run manually
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join('assets', 'icon.ico') if os.path.exists(os.path.join('assets', 'icon.ico')) else None,
)
