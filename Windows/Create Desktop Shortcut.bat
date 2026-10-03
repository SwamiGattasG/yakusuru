@echo off
rem Creates a "Yakusuru" shortcut (with icon) on your Desktop and in the Start menu.
setlocal
set "ROOT=%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = (Resolve-Path '%ROOT%').Path;" ^
  "$target = Join-Path $root 'Windows\Yakusuru.exe';" ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "foreach ($dir in @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {" ^
  "  $lnk = $ws.CreateShortcut((Join-Path $dir 'Yakusuru.lnk'));" ^
  "  $lnk.TargetPath = $target; $lnk.WorkingDirectory = Join-Path $root 'app';" ^
  "  $lnk.IconLocation = $target + ',0'; $lnk.Description = 'AI subtitles in any language';" ^
  "  $lnk.Save();" ^
  "  Remove-Item -ErrorAction SilentlyContinue (Join-Path $dir 'Language Interpreter.lnk') }"
if errorlevel 1 (
  echo Could not create the shortcut.
) else (
  echo Shortcut created on your Desktop and in the Start menu.
)
pause
