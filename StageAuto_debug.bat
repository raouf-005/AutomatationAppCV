@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo StageAuto en mode diagnostic : les messages s'affichent dans cette fenetre.
echo Le tableau de bord s'ouvre dans quelques secondes. Ferme cette fenetre pour arreter.
echo.
if not exist .venv\Scripts\python.exe (
  echo .venv introuvable : lance d'abord install.bat
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m stageauto %*
echo.
echo StageAuto s'est arrete. Envoie les lignes ci-dessus si tu as besoin d'aide.
pause
