@echo off
setlocal EnableExtensions
pushd "%~dp0" || goto failed
if not exist node_modules\vite\package.json (
  call npm.cmd ci
  if errorlevel 1 goto failed_in_project
)
uv sync --locked
if errorlevel 1 goto failed_in_project
call npm.cmd run build
if errorlevel 1 goto failed_in_project
if /i "%~1"=="electron" goto electron
if /i "%~1"=="tauri" goto tauri
uv run --locked python -m backend.launch
goto done

:electron
call npm.cmd run desktop:electron
goto done

:tauri
if exist src-tauri\target\debug\zhixue-tauri.exe (
  src-tauri\target\debug\zhixue-tauri.exe
) else (
  cargo run --locked --manifest-path src-tauri\Cargo.toml
)

:done
if errorlevel 1 goto failed_in_project
popd
exit /b 0

:failed_in_project
popd
:failed
echo Unable to start the demo. See README.md.
pause
exit /b 1
