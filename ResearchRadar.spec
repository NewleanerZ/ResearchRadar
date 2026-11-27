# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for full distributable Windows ResearchRadar bundle."""

from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)
MODEL_REV = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
STAGED_MODEL_DIR = ROOT / "build" / "staged_models" / "bge-small-en-v1.5" / MODEL_REV

datas = [
    (str(ROOT / "web" / "dist"), "web/dist"),
    (str(ROOT / "research_radar" / "migrations"), "research_radar/migrations"),
    (str(ROOT / "research_radar" / "pet-mascot.png"), "research_radar"),
    (str(ROOT / ".env.example"), "."),
]
if STAGED_MODEL_DIR.exists():
    datas.append((str(STAGED_MODEL_DIR), f"models/bge-small-en-v1.5/{MODEL_REV}"))

datas += collect_data_files("tzdata")
datas += collect_data_files("sentence_transformers")

hiddenimports = (
    collect_submodules("alembic")
    + collect_submodules("uvicorn")
    + collect_submodules("apscheduler")
    + collect_submodules("langgraph")
    + collect_submodules("mcp")
    + collect_submodules("sentence_transformers")
    + collect_submodules("sklearn")
    + [
        "sqlalchemy.dialects.sqlite",
        "sqlite3",
        "research_radar.cli",
        "research_radar.desktop",
        "research_radar.api",
        "research_radar.workflow",
    ]
)

a = Analysis(
    [str(ROOT / "app_launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["streamlit", "pytest", "IPython", "jupyter", "matplotlib", "tkinter"],
    noarchive=False,
)

# Pin OpenSSL 3.5.9 DLLs from base Python & PySide6 MSVC 14.44 runtime, and drop stray system ICU DLLs
_base_dlls = Path(sys.base_prefix) / "DLLs"
_pyside6_dir = Path(sys.prefix) / "Lib" / "site-packages" / "PySide6"
_drop_root_names = {"libssl-3-x64.dll", "libcrypto-3-x64.dll"}
_vc_runtime = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll")
a.binaries = [
    b for b in a.binaries
    if not (Path(b[0]).name.lower() in _drop_root_names or (Path(b[0]).parent == Path(".") and Path(b[0]).name.lower().startswith("icu")))
    and not (Path(b[0]).parent == Path(".") and Path(b[0]).name.lower() in _vc_runtime)
]
for _dll_name in ("libssl-3-x64.dll", "libcrypto-3-x64.dll"):
    _src_dll = _base_dlls / _dll_name
    if _src_dll.exists():
        a.binaries.append((_dll_name, str(_src_dll), "BINARY"))
for _vc_name in _vc_runtime:
    _src_vc = _pyside6_dir / _vc_name
    if _src_vc.exists():
        a.binaries.append((_vc_name, str(_src_vc), "BINARY"))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ResearchRadar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ResearchRadar",
)
