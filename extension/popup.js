chrome.action.getBadgeText({}).then(t => { document.getElementById('s').textContent = t ? 'Connecté à l\'application.' : 'Application non lancée (double-clic sur StageAuto.bat).'; });
