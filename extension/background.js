// StageAuto Bridge : relaie les commandes Chrome DevTools de l'application locale vers les onglets
// des fenêtres StageAuto (même principe que Claude in Chrome : chrome.debugger + sessions de l'utilisateur).
// Une fenêtre par « voie » : 0 = lecture des offres, 1 = candidatures LinkedIn, 2 = candidatures autres sites,
// pour que plusieurs candidatures avancent en même temps sans se voler l'onglet actif.
const URL_WS = 'ws://127.0.0.1:8765/ext';
let ws = null;
let wins = null;          // voie -> id de fenêtre
const attached = new Set();
const HOME = chrome.runtime.getURL('lane.html');  // onglet d'accueil épinglé : garde la fenêtre ouverte entre deux passages
const isHome = (t) => (t.url || t.pendingUrl || '').startsWith(HOME);

function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

// Reconnexion espacée quand l'application est fermée (30 s, 1 min, puis toutes les 2 min) : chaque tentative
// refusée laisse une ligne dans « Erreurs » de chrome://extensions, inutile d'en produire deux par minute.
// Connexion immédiate quand le tableau de bord s'ouvre ou via le bouton « Reconnecter » de l'extension.
async function backoffState() {
  const s = await chrome.storage.session.get(['fails', 'nextTry']);
  return {fails: s.fails || 0, nextTry: s.nextTry || 0};
}

async function noteFailure() {
  const {fails} = await backoffState();
  const n = fails + 1;
  const delay = Math.min(120, 30 * 2 ** Math.max(0, n - 1)) * 1000;
  await chrome.storage.session.set({fails: n, nextTry: Date.now() + delay});
}

function connect(force) {
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
  let opened = false;
  try { ws = new WebSocket(URL_WS); } catch (e) { ws = null; noteFailure(); return; }
  ws.onopen = () => {
    opened = true;
    chrome.storage.session.set({fails: 0, nextTry: 0});
    send({event: 'hello', version: chrome.runtime.getManifest().version});
    setBadge('on');
  };
  ws.onmessage = async (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    try { send({id: m.id, result: await handle(m)}); }
    catch (e) { send({id: m.id, error: String((e && e.message) || e)}); }
  };
  ws.onclose = () => {
    ws = null;
    setBadge('');
    if (!opened) noteFailure();  // l'application ne tourne pas : on espace les essais
  };
  ws.onerror = () => {};
}

async function maybeConnect() {
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
  const {nextTry} = await backoffState();
  if (Date.now() >= nextTry) connect();
}

async function connectNow() {
  await chrome.storage.session.set({fails: 0, nextTry: 0});
  connect();
}

function setBadge(t) {
  chrome.action.setBadgeText({text: t});
  chrome.action.setBadgeBackgroundColor({color: t ? '#1f7a4d' : '#888'});
}

async function loadWins() {
  if (wins === null) {
    const s = await chrome.storage.session.get('wins');
    wins = s.wins || {};
  }
  return wins;
}

async function ensureWindow(lane) {
  lane = String(lane || 0);
  await loadWins();
  if (wins[lane] !== undefined) {
    try { await chrome.windows.get(wins[lane]); return wins[lane]; } catch (e) { delete wins[lane]; }
  }
  const off = 40 * Number(lane);
  const url = `${HOME}?lane=${lane}`;
  let w;
  try {
    w = await chrome.windows.create({url, focused: false, width: 1280, height: 860, left: 40 + off, top: 30 + off});
  } catch (e) {  // petit écran : Chrome choisit lui-même la position
    w = await chrome.windows.create({url, focused: false});
  }
  if (w.tabs && w.tabs[0]) chrome.tabs.update(w.tabs[0].id, {pinned: true}).catch(() => {});
  wins[lane] = w.id;
  await chrome.storage.session.set({wins});
  return w.id;
}

async function ourTabs() {
  await loadWins();
  const out = [];
  for (const [lane, id] of Object.entries(wins)) {
    try {
      for (const t of await chrome.tabs.query({windowId: id}))
        if (!isHome(t)) out.push({tabId: t.id, url: t.url, title: t.title, active: t.active, opener: t.openerTabId, lane: Number(lane)});
    } catch (e) { /* fenêtre fermée */ }
  }
  return out;
}

// Retire, dans un onglet StageAuto, les cadres d'autres extensions (y compris dans les racines fantômes fermées)
function sweepForeignFrames(mine) {
  const shadowOf = (e) => (chrome.dom && chrome.dom.openOrClosedShadowRoot) ? chrome.dom.openOrClosedShadowRoot(e) : e.shadowRoot;
  let n = 0;
  const sweep = (root) => {
    root.querySelectorAll('iframe').forEach(f => {
      if (/^chrome-extension:\/\//.test(f.src || '') && !(f.src || '').startsWith(mine)) { f.remove(); n++; }
    });
    root.querySelectorAll('*').forEach(e => { const sr = shadowOf(e); if (sr) sweep(sr); });
  };
  sweep(document);
  return n;
}

async function guardTab(tabId) {
  try {
    const r = await chrome.scripting.executeScript({target: {tabId}, func: sweepForeignFrames, args: [chrome.runtime.getURL('')]});
    return (r && r[0] && r[0].result) || 0;
  } catch (e) { return 0; }
}

async function isOurWindow(windowId) {
  await loadWins();
  return Object.values(wins).includes(windowId);
}

async function attach(tabId) {
  if (attached.has(tabId)) return;
  try { await chrome.debugger.attach({tabId}, '1.3'); }
  catch (e) { if (!String(e).includes('already attached')) throw e; }
  attached.add(tabId);
  // la page se croit au premier plan : menus et champs qui réagissent au focus se comportent normalement
  chrome.debugger.sendCommand({tabId}, 'Emulation.setFocusEmulationEnabled', {enabled: true}).catch(() => {});
}

chrome.debugger.onDetach.addListener((src) => attached.delete(src.tabId));
chrome.tabs.onRemoved.addListener((id) => { attached.delete(id); send({event: 'tab_removed', tabId: id}); });
chrome.tabs.onCreated.addListener((t) => send({event: 'tab_created', tabId: t.id, opener: t.openerTabId, windowId: t.windowId}));

async function handle(m) {
  switch (m.cmd) {
    case 'ping': return 'pong';
    case 'tabs.create': {
      const lane = String(m.lane || 0);
      const w = await ensureWindow(lane);
      const t = await chrome.tabs.create({windowId: w, url: m.url || 'about:blank', active: true});
      return {tabId: t.id};
    }
    case 'tabs.list': return await ourTabs();
    case 'tabs.close': await chrome.tabs.remove(m.tabId); return true;
    case 'tabs.activate': await chrome.tabs.update(m.tabId, {active: true}); return true;
    case 'window.show': {  // « Voir Chrome » : fenêtres StageAuto au premier plan
      await loadWins();
      const lanes = m.lane === 'all' ? Object.keys(wins) : [String(m.lane || 0)];
      if (!lanes.length) lanes.push('0');
      for (const lane of lanes) {
        const w = await ensureWindow(lane);
        const info = await chrome.windows.get(w);
        await chrome.windows.update(w, info.state === 'minimized' ? {state: 'normal', focused: true} : {focused: true});
      }
      return lanes.length;
    }
    case 'cdp': {
      // Chrome refuse ou coupe le pilotage quand une autre extension insère un cadre dans la page : on retire ces
      // cadres, on se rattache et on recommence (sauf clavier/souris coupés en plein milieu, pour ne pas doubler un clic).
      for (let attempt = 0; ; attempt++) {
        try {
          await attach(m.tabId);
          // l'onglet passe au premier plan de SA fenêtre seulement pour la souris, le clavier et les captures
          if (/^Input\.|^Page\.captureScreenshot/.test(m.method)) {
            const t = await chrome.tabs.get(m.tabId).catch(() => null);
            if (t && !t.active) await chrome.tabs.update(m.tabId, {active: true}).catch(() => {});
          }
          return await chrome.debugger.sendCommand({tabId: m.tabId}, m.method, m.params || {});
        } catch (e) {
          const msg = String((e && e.message) || e);
          const blocked = /different extension|not attached|Cannot attach/i.test(msg);
          const cut = /Detached/i.test(msg) && !/^Input\./.test(m.method);
          if (attempt >= 3 || !(blocked || cut)) throw e;
          attached.delete(m.tabId);
          await chrome.debugger.detach({tabId: m.tabId}).catch(() => {});
          await guardTab(m.tabId);
          await new Promise(r => setTimeout(r, 250 * (attempt + 1)));
        }
      }
    }
    default: throw new Error('commande inconnue ' + m.cmd);
  }
}

chrome.alarms.create('keepalive', {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(() => {
  if (ws && ws.readyState === 1) send({event: 'ping'});
  else maybeConnect();
});
// Le tableau de bord vient de s'ouvrir : l'application tourne, connexion tout de suite
chrome.tabs.onUpdated.addListener((id, info, tab) => {
  if (info.status === 'complete' && (tab.url || '').startsWith('http://127.0.0.1:8765')) connectNow();
});
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg === 'reconnect') { connectNow(); reply(true); return; }
  if (msg && msg.q === 'isStageAutoTab') {  // guard.js : ne protéger que les onglets des fenêtres StageAuto
    isOurWindow(sender.tab && sender.tab.windowId).then(reply);
    return true;
  }
});
chrome.runtime.onStartup.addListener(connectNow);
chrome.runtime.onInstalled.addListener(connectNow);
maybeConnect();
