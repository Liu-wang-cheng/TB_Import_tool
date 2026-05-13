# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: 打包为单个 exe"""

import os

block_cipher = None

# 从 VERSION 文件读取版本号（唯一版本源）
_version_file = os.path.join(SPECPATH, 'VERSION')
with open(_version_file, 'r') as _f:
    VERSION = _f.read().strip()
EXE_NAME = '智能缺陷管理平台'

# 收集 data/ 目录下的 TF-IDF 训练模型（如果存在）
extra_datas = []
data_dir = os.path.join(SPECPATH, 'data')
if os.path.isdir(data_dir):
    extra_datas.append(('data', 'data'))

a = Analysis(
    ['gui_main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('configs', 'configs'),
        ('gui/resources/style.qss', 'gui/resources'),
        ('gui/resources/icon.ico', 'gui/resources'),
        ('VERSION', '.'),
    ] + extra_datas,
    hiddenimports=[
        'src',
        'src.models',
        'src.zentao_client',
        'src.teambition_client',
        'src.sync_engine',
        'src.config_loader',
        'src.config_resolver',
        'src.utils',
        'src.classifier',
        'src.extractor',
        'src.source_client',
        'src.source_factory',
        'src.zentao_adapter',
        'gui.updater',
        'gui.workers',
        'dingtalk',
        'dingtalk.bot',
        'dingtalk.server',
        'flask',
        'tools',
        'tools.query_ids',
        'tools.export_bugs',
        'PyQt6',
        'PyQt6.QtCore',
        'PyQt6.QtWidgets',
        'PyQt6.QtGui',
        'yaml',
        'bs4',
        'requests',
        'jwt',
        'jieba',
        'pypinyin',
        'sklearn',
        'sklearn.feature_extraction',
        'sklearn.feature_extraction.text',
        'sklearn.metrics',
        'sklearn.metrics.pairwise',
        'scipy',
        'scipy.sparse',
        'numpy',
        'joblib',
        'oss2',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5'],
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
    name=EXE_NAME,
    icon=os.path.join(SPECPATH, 'gui', 'resources', 'icon.ico'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir='.',
    console=False,  # --windowed: 不显示控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
