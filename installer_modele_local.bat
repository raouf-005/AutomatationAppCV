@echo off
chcp 65001 >nul
rem Installe le modèle local de secours de StageAuto (4 milliards de paramètres, ~2,6 Go) et le règle pour l'application.
where ollama >nul 2>nul
if errorlevel 1 (
  echo Ollama n'est pas installé : installation via winget...
  winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
  set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Ollama"
)
echo Téléchargement du modèle de base qwen3:4b...
ollama pull qwen3:4b
if errorlevel 1 (
  echo qwen3:4b indisponible : essai avec qwen2.5:3b
  ollama pull qwen2.5:3b
  powershell -NoProfile -Command "(Get-Content '%~dp0ollama\Modelfile') -replace '^FROM .*','FROM qwen2.5:3b' | Set-Content '%~dp0ollama\Modelfile.local'"
  ollama create stageauto -f "%~dp0ollama\Modelfile.local"
) else (
  ollama create stageauto -f "%~dp0ollama\Modelfile"
)
echo.
echo Modèle « stageauto » prêt. Dans StageAuto : Réglages → IA → « Enregistrer et tester l'IA » sur « Modèle local : Ollama »
echo pour voir l'estimation du temps par candidature.
pause
