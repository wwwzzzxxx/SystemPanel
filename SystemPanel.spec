# -*- mode: python ; coding: utf-8 -*-
"""SystemPanel 单文件打包：onefile + 无控制台 + 默认提权。"""
import os

PROJECT = SPECPATH  # spec 文件所在目录即项目根

# conda 把 tcl/tk/ffi 放在 Library\bin，PyInstaller 默认扫不到，需要显式喂给它。
# 路径从 CONDA_PREFIX 推断；不在 conda 环境下运行时，用 SYSTEMPANEL_CONDA_BIN
# 指向 <conda_env>\Library\bin。
_CONDA_BIN = os.environ.get("SYSTEMPANEL_CONDA_BIN") or os.path.join(
    os.environ.get("CONDA_PREFIX", ""), "Library", "bin"
)
if not os.path.isdir(_CONDA_BIN):
    raise SystemExit(
        "找不到 conda 的 Library\\bin（当前尝试：%r）\n"
        "请在 conda 环境下运行 pyinstaller；或先设置环境变量：\n"
        "    set SYSTEMPANEL_CONDA_BIN=<conda_env>\\Library\\bin" % _CONDA_BIN
    )

a = Analysis(
    ["run_panel.py"],
    pathex=[PROJECT],
    binaries=[
        (os.path.join(_CONDA_BIN, dll), ".")
        for dll in ("ffi-8.dll", "tcl86t.dll", "tk86t.dll")
    ],
    datas=[
        ("_engine/LibreHardwareMonitor/*.dll", "_engine/LibreHardwareMonitor"),
    ],
    hiddenimports=["clr", "pynvml", "win32gui", "win32con", "win32event", "win32api"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SystemPanel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,  # 默认申请管理员权限（温度可用）
)
