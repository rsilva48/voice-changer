# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_all, collect_dynamic_libs, collect_submodules
import glob
import sys
import os.path
import site

sys.setrecursionlimit(sys.getrecursionlimit() * 5)

backend = os.environ.get('BACKEND', 'cpu')

with open('edition.txt', 'w') as f:
    if backend == 'cpu':
      f.write('CPU')
    elif backend == 'dml':
      f.write('DirectML')
    elif backend == 'cuda':
      f.write('NVIDIA-CUDA')
    elif backend == 'rocm':
      f.write('AMD-ROCm')
    else:
      f.write('-')

datas = [('../client/modern-gui/dist', './dist'), ('./edition.txt', '.')]

if 'BUILD_NAME' in os.environ:
  with open('version.txt', 'w') as f:
      f.write(os.environ['BUILD_NAME'])
  datas += [('./version.txt', '.')]
datas += collect_data_files('onnxscript', include_py_files=True)

binaries = []
if backend == 'dml':
  binaries += collect_dynamic_libs('torch_directml')
elif backend == 'rocm':
  binaries += collect_dynamic_libs('torch')

hiddenimports = ['app']
hiddenimports += collect_submodules('scipy') # Fix "ModuleNotFoundError: No module named 'scipy._lib.*'"

tmp_ret = collect_all('onnxruntime') # Fix "ModuleNotFoundError: No module named 'onnxruntime.transformers.*'"
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

if backend == 'rocm':
  # --- rocm_sdk Python packages (must be importable at runtime) ---
  hiddenimports += collect_submodules('rocm_sdk')
  hiddenimports += collect_submodules('rocm_sdk_core')
  hiddenimports += ['_rocm_sdk_core', '_rocm_sdk_libraries_custom']
  datas += collect_data_files('rocm_sdk', include_py_files=True)
  datas += collect_data_files('rocm_sdk_core', include_py_files=True)
  datas += collect_data_files('_rocm_sdk_core', include_py_files=True)
  datas += collect_data_files('_rocm_sdk_libraries_custom', include_py_files=True)

  # --- ROCm runtime DLLs and GPU kernel data ---
  # rocm_sdk.find_libraries() resolves DLLs via:
  #   Path(_rocm_sdk_<pkg>.__file__).parent / "bin" / <dll_pattern>
  # So DLLs must land at <bundle>/_rocm_sdk_<pkg>/bin/ in the dist tree.
  for _sp in site.getsitepackages():
    for _pkg in ['_rocm_sdk_core', '_rocm_sdk_libraries_custom']:
      _bin_dir = os.path.join(_sp, _pkg, 'bin')
      if not os.path.isdir(_bin_dir):
        continue
      for _f in glob.glob(os.path.join(_bin_dir, '**', '*'), recursive=True):
        if os.path.isfile(_f):
          _rel_dest = os.path.relpath(os.path.dirname(_f), _sp)
          datas.append((_f, _rel_dest))

a = Analysis(
    ['client.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=['./pyinstaller-hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MMVCServerSIO',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="./vc_64.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MMVCServerSIO',
)
