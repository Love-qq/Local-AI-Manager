# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files


a = Analysis(
    ['local_ai_manager.py'],
    pathex=[],
    binaries=[],
    # Словари i18n — данные, а не модули: без них EXE откроется без переводов.
    # Прямые слэши: PyInstaller принимает их и на Windows, и на Linux, поэтому
    # один и тот же spec собирается в обеих системах без правок.
    datas=collect_data_files('certifi') + [('lamcore/i18n/ru.json', 'lamcore/i18n'),
           ('lamcore/i18n/en.json', 'lamcore/i18n'),
           ('assets/local_ai_manager_icon.ico', 'assets')],
    hiddenimports=['ai_bridge'],
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
    name='LocalAIManager',
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
    version='version_info.txt',
    icon='assets/local_ai_manager_icon.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='LocalAIManager',
)
