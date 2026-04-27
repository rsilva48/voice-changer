#Requires -Version 5.1
<#
.SYNOPSIS
    Installs Voice Changer Server with AMD ROCm support on Windows.

.DESCRIPTION
    Creates a Python 3.12 virtual environment and installs PyTorch with native
    AMD ROCm 7.2.1 GPU acceleration for RDNA2/RDNA3/RDNA4 GPUs (RX 6000/7000/9000 series).

    Prerequisites:
      - Python 3.12 installed via the Windows Python installer (py launcher available)
      - AMD Adrenalin Edition driver 26.2.2 or newer
      - Windows 10/11 64-bit

    Reference:
      https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/install/installrad/windows/install-pytorch.html

.NOTES
    ROCm on Windows uses the HIP layer that exposes itself through torch.cuda.*
    so torch.cuda.is_available() returns True when setup is correct.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ------------------------------------------------------------------ #
#  Helpers
# ------------------------------------------------------------------ #
function Write-Step([string]$msg) {
    Write-Host "`n[*] $msg" -ForegroundColor Cyan
}

function Write-OK([string]$msg) {
    Write-Host "    [OK] $msg" -ForegroundColor Green
}

function Write-Warn([string]$msg) {
    Write-Host "    [!]  $msg" -ForegroundColor Yellow
}

function Write-Fail([string]$msg) {
    Write-Host "`n[ERROR] $msg" -ForegroundColor Red
}

function Invoke-Step([string]$desc, [scriptblock]$block) {
    Write-Step $desc
    try { & $block } catch {
        Write-Fail $_
        if ([Environment]::UserInteractive) { Read-Host "Press Enter to exit" }
        exit 1
    }
}

# ------------------------------------------------------------------ #
#  Banner
# ------------------------------------------------------------------ #
Write-Host @"

===============================================================
  Voice Changer Server — AMD ROCm Windows Installer
    ROCm 7.2.1 | PyTorch 2.9.1 | Python 3.12
===============================================================
"@ -ForegroundColor Magenta

# ------------------------------------------------------------------ #
#  0. Make sure we are in the server directory
# ------------------------------------------------------------------ #
$ScriptDir = Split-Path -Parent $PSCommandPath
Set-Location $ScriptDir

if (-not (Test-Path "main.py")) {
    Write-Fail "This script must be run from the 'server' directory."
    exit 1
}

# ------------------------------------------------------------------ #
#  1. Locate Python 3.12 via the py launcher
# ------------------------------------------------------------------ #
Invoke-Step "Locating Python 3.12" {
    $pyExe = $null

    # Try py launcher first (Windows installer sets this up)
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $ver = py -3.12 --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $ver -match "3\.12") {
            $pyExe = "py -3.12"
            Write-OK "Found via py launcher: $ver"
        }
    }

    # Fall back to python3.12 / python in PATH
    if (-not $pyExe) {
        foreach ($cmd in @("python3.12", "python3", "python")) {
            if (Get-Command $cmd -ErrorAction SilentlyContinue) {
                $ver = & $cmd --version 2>&1
                if ($ver -match "3\.12") {
                    $pyExe = $cmd
                    Write-OK "Found via PATH: $ver"
                    break
                }
            }
        }
    }

    if (-not $pyExe) {
        throw "Python 3.12 not found. Install it from https://www.python.org/downloads/ and ensure the 'py' launcher is available."
    }

    # Export for later steps
    $script:PyExe = $pyExe
}

# ------------------------------------------------------------------ #
#  2. Create virtual environment
# ------------------------------------------------------------------ #
$VenvDir = Join-Path $ScriptDir "venv"

Invoke-Step "Creating virtual environment at '$VenvDir'" {
    if (Test-Path $VenvDir) {
        Write-Warn "Existing venv found — renaming to venv_old and creating a fresh one."
        $oldVenv = "$VenvDir`_old"
        if (Test-Path $oldVenv) { Remove-Item -Recurse -Force $oldVenv -ErrorAction SilentlyContinue }
        Rename-Item $VenvDir $oldVenv -ErrorAction Stop
        Write-Warn "Old venv kept at '$oldVenv'. You can delete it manually once installation succeeds."
    }

    $createCmd = "$($script:PyExe) -m venv `"$VenvDir`""
    Invoke-Expression $createCmd

    if (-not (Test-Path "$VenvDir\Scripts\python.exe")) {
        throw "Virtual environment creation failed."
    }
    Write-OK "Virtual environment created."
}

$PythonExe = "$VenvDir\Scripts\python.exe"

# Convenience wrapper so we always target the venv python -m pip
function Invoke-Pip([string[]]$PipArgs) {
    & $PythonExe -m pip @PipArgs
    if ($LASTEXITCODE -ne 0) { throw "pip command failed: pip $PipArgs" }
}

# ------------------------------------------------------------------ #
#  3. Upgrade pip / setuptools / wheel
# ------------------------------------------------------------------ #
Invoke-Step "Upgrading pip, setuptools, wheel" {
    Invoke-Pip @("install", "--upgrade", "pip", "setuptools", "wheel")
    Write-OK "pip upgraded."
}

# ------------------------------------------------------------------ #
#  4. Install AMD ROCm SDK wheels
# ------------------------------------------------------------------ #
Invoke-Step "Installing AMD ROCm 7.2.1 SDK (this may take several minutes)" {
    $rocmSdkWheels = @(
        "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/rocm_sdk_core-7.2.1-py3-none-win_amd64.whl",
        "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/rocm_sdk_devel-7.2.1-py3-none-win_amd64.whl",
        "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl",
        "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/rocm-7.2.1.tar.gz"
    )

    Invoke-Pip (@("install", "--no-cache-dir") + $rocmSdkWheels)
    Write-OK "ROCm SDK installed."
}

# ------------------------------------------------------------------ #
#  5. Install PyTorch + torchaudio + torchvision (ROCm cp312 wheels)
# ------------------------------------------------------------------ #
Invoke-Step "Installing PyTorch 2.9.1 + ROCm 7.2.1 (cp312 wheels, may take several minutes)" {
    $torchWheels = @(
        "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/torch-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl",
        "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/torchaudio-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl",
        "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/torchvision-0.24.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl"
    )

    Invoke-Pip (@("install", "--no-cache-dir") + $torchWheels)
    Write-OK "PyTorch (ROCm) installed."
}

# ------------------------------------------------------------------ #
#  6. Install common requirements
# ------------------------------------------------------------------ #
Invoke-Step "Installing common requirements" {
    Invoke-Pip @("install", "-r", "requirements-common.txt")
    Write-OK "Common requirements installed."
}

# ------------------------------------------------------------------ #
#  7. Install ROCm-Windows-specific requirements (faiss, onnx, etc.)
# ------------------------------------------------------------------ #
Invoke-Step "Installing ROCm-Windows-specific requirements" {
    Invoke-Pip @("install", "--no-cache-dir", "-r", "requirements-rocm-windows.txt")
    Write-OK "ROCm-Windows requirements installed."
}

# ------------------------------------------------------------------ #
#  8. Verify PyTorch + GPU detection
# ------------------------------------------------------------------ #
Invoke-Step "Verifying PyTorch and ROCm GPU detection" {
    Write-Host ""

    # Basic import
    $importOk = & $PythonExe -c "import torch; print('torch', torch.__version__)" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "PyTorch import failed: $importOk" }
    Write-OK $importOk

    # CUDA / HIP availability
    $cudaAvail = & $PythonExe -c "import torch; print('CUDA/ROCm available:', torch.cuda.is_available())" 2>&1
    Write-Host "    $cudaAvail"
    if ($cudaAvail -notmatch "True") {
        Write-Warn "GPU not detected via torch.cuda.is_available()."
        Write-Warn "Make sure AMD driver 26.2.2+ is installed and the GPU is RDNA2 or newer."
    }

    # Device name
    $devName = & $PythonExe -c @"
import torch
if torch.cuda.is_available():
    print('GPU device [0]:', torch.cuda.get_device_name(0))
else:
    print('No GPU detected.')
"@ 2>&1
    Write-Host "    $devName"

    # Full env report
    Write-Host ""
    Write-Host "    -- collect_env --" -ForegroundColor DarkGray
    & $PythonExe -m torch.utils.collect_env 2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
}

# ------------------------------------------------------------------ #
#  Done
# ------------------------------------------------------------------ #
Write-Host @"

===============================================================
  Installation complete!
===============================================================
  Backend  : AMD ROCm 7.2.1 (Windows)
  PyTorch  : 2.9.1+rocm7.2.1
  Python   : 3.12

  To start the server run:
      .\vc_startup.bat
      -- or --
      $VenvDir\Scripts\python.exe main.py

  Note: ROCm exposes itself via torch.cuda.* (HIP compatibility
  layer). The existing CUDA code paths work without modification.
===============================================================
"@ -ForegroundColor Green

if ([Environment]::UserInteractive) { Read-Host "Press Enter to exit" }
