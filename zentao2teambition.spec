# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: 打包为目录模式（onedir），DLL 直接在 exe 旁边"""

import os

block_cipher = None

# 从 VERSION 文件读取版本号（唯一版本源）
_version_file = os.path.join(SPECPATH, 'VERSION')
with open(_version_file, 'r') as _f:
    VERSION = _f.read().strip()
EXE_NAME = '智能缺陷管理平台'

# 收集 data/ 下的必要运行时文件（排除约 42MB 的 PDF 和 DRC 配置 JSON）
# PyInstaller datas 格式: (source, dest_dir)，source 相对于 SPECPATH
_DATA_RUNTIME = [
    'data/classifier_model.pkl',
    'data/sweeper_knowledge_base.yaml',
    'data/knowledge_feedback.yaml',
    'data/knowledge_base_patch.yaml',
    'data/pdf_flowchart_knowledge.yaml',
]
extra_datas = [(f, 'data') for f in _DATA_RUNTIME
               if os.path.isfile(os.path.join(SPECPATH, f))]

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
        'src.ai_log_analyzer',
        'src.log_analysis_integration',
        'src.fault_pattern_library',
        'src.prompt_builder',
        'src.knowledge_base',
        'src.knowledge_rag',
        'src.collaborative_learning',
        'src.html_report_generator',
        'src.vision_analyzer',
        'src.vision_integration',
        'src.tb_web_downloader',
        'src.zentao_video_downloader',
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
        'cv2',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PyQt5',
        'pandas',           # 未被代码使用，PyInstaller 自动拉入但非必需
        'matplotlib',       # 未被代码使用，PyInstaller 自动拉入但非必需
        'PIL',              # Pillow 图像库，未被代码使用
        'pytest',           # 测试框架，运行时不需要
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=EXE_NAME,
    icon=os.path.join(SPECPATH, 'gui', 'resources', 'icon.ico'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
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
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name=EXE_NAME,
)
