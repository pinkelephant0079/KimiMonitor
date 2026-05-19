# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for KimiMonitor
# macOS menu bar app built with Python + rumps

import sys
sys.path.insert(0, '/Users/sen/KimiBackups/KimiMonitor')

a = Analysis(
    ['app.py', 'version.py'],
    pathex=['/Users/sen/KimiBackups/KimiMonitor'],
    binaries=[],
    datas=[],
    hiddenimports=[
        # rumps & requests
        'rumps',
        'requests',
        # requests 字符编码依赖（PyInstaller 可能遗漏）
        'charset_normalizer',
        'chardet',
        'certifi',
        'urllib3',
        # PyObjC 核心框架
        'Foundation',
        'AppKit',
        'Quartz',
        'PyObjCTools.AppHelper',
        'PyObjCTools',
        # PyObjC 框架子模块（PyInstaller hook 可能遗漏）
        'Foundation._Foundation',
        'AppKit._AppKit',
        'Quartz.CoreGraphics',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除不需要的模块以减小体积
        'matplotlib',
        'numpy',
        'pandas',
        'tkinter',
        'PyQt5',
        'PyQt6',
        'PySide2',
        'PySide6',
        'wx',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='KimiMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,  # macOS 需要，用于处理文件拖放/open事件
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='applet.icns',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='KimiMonitor',
)

app = BUNDLE(
    coll,
    name='KimiMonitor.app',
    icon='applet.icns',
    bundle_identifier='com.sen.kimimonitor',
    info_plist={
        'LSUIElement': True,                           # 菜单栏应用，不显示 Dock 图标
        'CFBundleName': 'KimiMonitor',
        'CFBundleDisplayName': 'KimiMonitor',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'CFBundlePackageType': 'APPL',
        'NSHighResolutionCapable': True,               # 支持 Retina
        'LSMinimumSystemVersion': '11.0',              # 最低 macOS 11.0 (Big Sur)
        'CFBundleDevelopmentRegion': 'zh-CN',
    },
)
