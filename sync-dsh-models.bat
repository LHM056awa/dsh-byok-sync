@echo off
rem ============================================================
rem  Auto-sync the DSH third-party endpoint model lists
rem  Equivalent to the VS Code task "Sync DSH Models"
rem  (.vscode/tasks.json):  python -m dsh_byok_sync --all
rem
rem  - Fetches every llm-pi-ai route's model list and writes the
rem    config file in this one run: no preview, no Y/N confirm
rem  - Deletes stale models (runs WITHOUT --no-delete)
rem  - The window stays open afterwards so the report is readable
rem
rem  Double-click to run, or:  sync-dsh-models.bat
rem
rem  To sync a single route, or to skip deletion, run the CLI
rem  yourself instead of this shortcut:
rem      python -m dsh_byok_sync --provider iris --no-delete
rem
rem  KEEP THIS FILE ASCII-ONLY AND DO NOT ADD "chcp 65001" HERE.
rem  cmd.exe finds its next line in a batch file by byte offset; a
rem  UTF-8 file that changes the code page mid-file desynchronises
rem  that offset and later lines come out mangled ("... is not
rem  recognized as an internal or external command"). The report is
rem  Chinese but Python writes it as Unicode straight to the console,
rem  so it needs no code page of its own.
rem ============================================================
setlocal
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%src;%PYTHONPATH%"

rem --- pick an interpreter: python first, then py -3 -----------
set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] No Python interpreter found ^(neither python nor py -3^).
        echo         Install Python 3.10+ and put it on PATH.
        echo.
        pause
        exit /b 2
    )
    set "PY=py -3"
)

echo === dsh-byok-sync: DSH third-party endpoint model sync ===
echo Python : %PY%
echo Time   : %date% %time%
echo.

rem --- the sync itself: one run, straight to the file ----------
%PY% -m dsh_byok_sync --all
set "RC=%ERRORLEVEL%"

echo.
echo === done ^(exit code %RC%^) ===
rem exit code: 0 = all ok, 1 = one or more endpoints failed
pause
endlocal
exit /b %RC%
