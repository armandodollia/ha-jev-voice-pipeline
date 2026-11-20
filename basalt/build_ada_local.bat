@echo off
rem Builds the Basalt Ada fork (sm_89, RTX 40) for the kit's Basalt engine (docs\engines\basalt-ada.md).
rem Copy into your basalt_ada checkout and set the three paths. Output: bin\basalt.exe; log: build_local.log.
rem Needs Visual Studio 2022 (Desktop development with C++), a CUDA 13 toolkit (the no-admin redistributable tree
rem works), and a Python venv with Basalt's requirements.txt plus ninja.
setlocal
cd /d "%~dp0"
set "CUDA=C:\Users\<you>\llm\cuda-redist\cuda-13.4.3"
set "VENV=C:\Users\<you>\llm\basalt_venv"
set "VCVARS=C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
set "LOG=%~dp0build_local.log"
echo ==== build %date% %time% ==== > "%LOG%"
set "VSCMD_SKIP_SENDTELEMETRY=1"
call "%VCVARS%" > nul 2>&1
set "CUDA_PATH=%CUDA%"
set "PATH=%CUDA%\bin;%CUDA%\nvvm\bin;%VENV%\Scripts;%PATH%"
nvcc --version >> "%LOG%" 2>&1
cmake -S . -B build-ada -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_ARCHITECTURES=89 ^
  -DCMAKE_CUDA_COMPILER="%CUDA%/bin/nvcc.exe" -DCUDAToolkit_ROOT="%CUDA%" ^
  -DPython3_EXECUTABLE="%VENV%/Scripts/python.exe" -DBASALT_MODELS=qwen4exp >> "%LOG%" 2>&1
if errorlevel 1 (echo CONFIGURE FAILED >> "%LOG%" & goto :eof)
cmake --build build-ada -j 12 -- -k 0 >> "%LOG%" 2>&1
if errorlevel 1 (echo BUILD FAILED >> "%LOG%" & goto :eof)
echo BUILD OK >> "%LOG%"
