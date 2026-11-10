@echo off
chcp 65001 >nul
rem Installe le modèle local de StageAuto (DeepSeek-V4-Pro-Qwen3.5-9B, ~5,8 Go) et crée le modèle réglé « stageauto ».
set "BASE=hf.co/Jackrong/DeepSeek-V4-Pro-Qwen3.5-9B-MTP-GGUF:Q4_K_M"
where ollama >nul 2>nul
if errorlevel 1 (
  echo Ollama n'est pas installé : installation via winget...
  winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
  set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Ollama"
)
echo Téléchargement du modèle %BASE% (environ 5,8 Go, une seule fois)...
ollama pull %BASE%
if errorlevel 1 goto secours
ollama create stageauto -f "%~dp0ollama\Modelfile"
if errorlevel 1 goto secours
goto fin

:secours
echo.
echo Ce modèle n'a pas pu être installé (Ollama trop ancien ? mets-le à jour : winget upgrade Ollama.Ollama, puis relance).
echo En attendant, installation du modèle de secours qwen3:4b (environ 2,5 Go).
ollama pull qwen3:4b
powershell -NoProfile -Command "(Get-Content -Encoding UTF8 '%~dp0ollama\Modelfile') -replace '^FROM .*','FROM qwen3:4b' | Set-Content -Encoding UTF8 '%~dp0ollama\Modelfile.local'"
ollama create stageauto -f "%~dp0ollama\Modelfile.local"

:fin
echo.
echo Modèle « stageauto » prêt. Dans StageAuto : Réglages, Intelligence artificielle, « Enregistrer et tester l'IA » sur
echo « Modèle local : Ollama » pour voir l'estimation du temps par candidature.
pause
