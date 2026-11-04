const lane = new URLSearchParams(location.search).get('lane') || '0';
const names = {'0': 'Lecture des offres', '1': 'Candidatures LinkedIn', '2': 'Candidatures sur les autres sites'};
document.getElementById('t').textContent = names[lane] || 'Fenêtre de travail';
document.title = 'StageAuto — ' + (names[lane] || 'fenêtre');
