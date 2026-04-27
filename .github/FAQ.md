# Frequently Asked Questions
Please read this FAQ before asking or making a bug report.

### General fixes:
*Do all these steps so you can fix some issues.*
- Restart the application
- Go to your Windows %AppData% (Win + R, then put %appdata% and press Enter) and delete the "**voice-changer-native-client**" folder
- Extract your .zip to a new location and avoid folders with space or specials characters (also avoid long file paths)
- If you don't have a GPU or have a too old GPU, try using the [Colab Version](https://colab.research.google.com/github/w-okada/voice-changer/blob/master/Realtime_Voice_Changer_on_Colab.ipynb) instead

### 1. AMD GPU don't appear or not working (DirectML)
> Please download the **latest DirectML version**, use the **f0 det. rmvpe_onnx** and .ONNX models only! (.pth models do not work properly, use the "Export to ONNX" it can take a while)

### 2. AMD GPU — Using ROCm on Windows (RX 6000/7000/9000 series)
> The DirectML version has limitations on newer AMD RDNA GPUs. For AMD Radeon RX 6000, RX 7000 and RX 9000 series you can use **native ROCm GPU acceleration** by running from source with the ROCm Windows installer.
>
> **Requirements:**
> - AMD Adrenalin Edition driver **26.1.1 or later**
> - **Python 3.12** (from [python.org](https://www.python.org/downloads/), NOT the Microsoft Store)
>
> **Setup:**
> 1. Clone the repository and navigate to the `server` folder.
> 2. Run `vc_install.bat` and choose option **5 (ROCm for Windows)**.
> 3. The script will install ROCm SDK 7.2, PyTorch 2.9.1+rocm and all dependencies automatically (~3 GB).
> 4. After installation, run `vc_startup.bat` to start the server.
>
> **Notes:**
> - PyTorch `.pth` models run on the GPU. ONNX models (`rmvpe_onnx`, `fcpe_onnx`, ONNX inferencers) run on the CPU (DirectML is unstable on RDNA3/4 under the current ONNX Runtime version). For best GPU utilization use non-ONNX pitch extractors (`rmvpe`, `fcpe`, `crepe_full`).

### 3. NVidia GPU don't appear or not working
> Make sure that the [NVidia CUDA Toolkit](https://developer.nvidia.com/cuda-downloads) drivers are installed on your PC and up-to-date

### 4. High CPU usage
> Decrease your EXTRA value and put the index feature to 0

### 5. High Latency
> Decrease your chunk value until you find a good mix of quality and response time

### 6. I'm hearing my voice without changes
> Make sure to disable **passthru** mode
