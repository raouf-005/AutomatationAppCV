@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================================
echo   Installation de StageAuto
echo ================================================

rem --- 1. Python ---
where py >nul 2>nul
if errorlevel 1 (
  echo [1/6] Installation de Python 3.12...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  echo.
  echo Python vient d'etre installe : ferme cette fenetre et relance install.bat.
  pause
  exit /b 0
)
echo [1/6] Python OK

rem --- 2. Environnement et dependances ---
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip >nul
echo [2/6] Installation des dependances Python...
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 (echo ERREUR pip & pause & exit /b 1)

rem --- 3. LaTeX (MiKTeX) pour le CV et la lettre ---
where pdflatex >nul 2>nul
if errorlevel 1 (
  echo [3/6] Installation de MiKTeX, LaTeX...
  winget install -e --id MiKTeX.MiKTeX --accept-package-agreements --accept-source-agreements
)
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\MiKTeX\miktex\bin\x64;C:\Program Files\MiKTeX\miktex\bin\x64"
initexmf --set-config-value=[MPM]AutoInstall=1 >nul 2>nul
initexmf --admin --set-config-value=[MPM]AutoInstall=1 >nul 2>nul
echo [3/6] LaTeX OK

rem --- 4. Outils de controle ATS (facultatifs : sinon controle allege) ---
echo [4/6] Outils PDF (qpdf, poppler)...
where qpdf >nul 2>nul || winget install -e --id QPDF.QPDF --accept-package-agreements --accept-source-agreements >nul 2>nul
where pdftotext >nul 2>nul || winget install -e --id oschwartz10612.Poppler --accept-package-agreements --accept-source-agreements >nul 2>nul

rem --- 5. Dossiers et passages planifies 11h / 19h ---
echo [5/6] Dossiers et planification...
if not exist "%USERPROFILE%\Downloads\Candidatures\envoyees" mkdir "%USERPROFILE%\Downloads\Candidatures\envoyees"
schtasks /Create /F /SC DAILY /ST 11:00 /TN "StageAuto 11h" /TR "\"%~dp0StageAuto.bat\" --run --no-open" >nul
schtasks /Create /F /SC DAILY /ST 19:00 /TN "StageAuto 19h" /TR "\"%~dp0StageAuto.bat\" --run --no-open" >nul

rem --- 6. Auto-test (compile un CV d'essai ; MiKTeX telecharge les paquets manquants la premiere fois) ---
echo [6/6] Auto-test...
.venv\Scripts\python -m stageauto.selftest

echo.
echo ================================================
echo   Derniere etape : l'extension Chrome
echo ================================================
echo  1. Dans Chrome, ouvre chrome://extensions
echo  2. Active le "Mode developpeur" (en haut a droite)
echo  3. "Charger l'extension non empaquetee" puis choisis le dossier :
echo       %~dp0extension
echo  4. Lance StageAuto.bat (double-clic). Le tableau de bord s'ouvre.
echo.
explorer "%~dp0extension"
pause
