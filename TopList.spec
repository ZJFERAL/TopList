# -*- mode: python ; coding: utf-8 -*-
"""TopList PyInstaller 打包配置。"""
import os

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('web', 'web'),  # web/ 目录整体作为数据文件打入
    ],
    hiddenimports=[
        'webview.platforms.winforms',
    ],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TopList',
    debug=False,
    console=False,  # GUI 应用，不弹控制台
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    onefile=False,  # 输出单目录（WebView2 依赖较多文件）
    icon='tododesktop.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name='TopList',
)