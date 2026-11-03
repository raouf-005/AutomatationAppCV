function show() {
  chrome.action.getBadgeText({}).then(t => {
    document.getElementById('s').textContent = t ? 'Connecté à l\'application.' : 'Application non lancée (double-clic sur StageAuto.bat).';
  });
}
document.getElementById('r').onclick = () => chrome.runtime.sendMessage('reconnect').then(() => setTimeout(show, 1500));
show();
