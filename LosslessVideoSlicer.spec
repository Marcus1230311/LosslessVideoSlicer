# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets']
hiddenimports += collect_submodules('PySide6.QtMultimedia')


a = Analysis(
    ['C:/Users/yanwu/Desktop/ai tools/LosslessVideoSlicer/LosslessVideoSlicer/entry.py'],
    pathex=['C:/Users/yanwu/Desktop/ai tools/LosslessVideoSlicer/LosslessVideoSlicer'],
    binaries=[('bin/ffmpeg.exe', 'bin'), ('bin/ffprobe.exe', 'bin')],
    datas=[('assets', 'assets')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LosslessVideoSlicer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='LosslessVideoSlicer',
)
