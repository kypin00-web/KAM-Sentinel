# scripts/gpu_bench.spec
# PyInstaller spec for KAM_GPU_Bench.exe
#
# Build from project root:
#   python -m PyInstaller scripts/gpu_bench.spec
#
# Output: dist/KAM_GPU_Bench.exe

import os
block_cipher = None

# SPECPATH = directory of this spec file (scripts/).
# All relative paths in Analysis() resolve to SPECPATH, not CWD.
_icon = os.path.join(SPECPATH, '..', 'assets', 'icon.ico')

a = Analysis(
    ['gpu_bench.py'],
    pathex=[os.path.join(SPECPATH, '..')],
    binaries=[],
    datas=[],
    hiddenimports=[
        'moderngl',
        'moderngl.mgl',
        'glcontext',
        'glcontext.wgl',
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
    icon=_icon if os.path.exists(_icon) else None,
)
