// StageAuto Bridge : relaie les commandes Chrome DevTools de l'application locale vers les onglets
// des fenêtres StageAuto (même principe que Claude in Chrome : chrome.debugger + sessions de l'utilisateur).
// Une fenêtre par « voie » : 0 = lecture des offres, 1 = candidatures LinkedIn, 2 = candidatures autres sites,
// pour que plusieurs candidatures avancent en même temps sans se voler l'onglet actif.
const URL_WS = 'ws://127.0.0.1:8765/ext';
let ws = null;
let wins = null;          // voie -> id de fenêtre
const initialTabs = {};   // voie -> onglet vide créé avec la fenêtre
const attached = new Set();

function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

function connect() {
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
  try { ws = new WebSocket(URL_WS); } catch (e) { ws = null; return; }
  ws.onopen = () => { send({event: 'hello', version: chrome.runtime.getManifest().version}); setBadge('on'); };
  ws.onmessage = async (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    try { send({id: m.id, result: await handle(m)}); }
    catch (e) { send({id: m.id, error: String((e && e.message) || e)}); }
  };
  ws.onclose = () => { ws = null; setBadge(''); };
  ws.onerror = () => {};
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
  let w;
  try {
    w = await chrome.windows.create({url: 'about:blank', focused: false, width: 1280, height: 860, left: 40 + off, top: 30 + off});
  } catch (e) {  // petit écran : Chrome choisit lui-même la position
    w = await chrome.windows.create({url: 'about:blank', focused: false});
  }
  wins[lane] = w.id;
  initialTabs[lane] = w.tabs && w.tabs[0] ? w.tabs[0].id : null;
  await chrome.storage.session.set({wins});
  return w.id;
}

async function ourTabs() {
  await loadWins();
  const out = [];
  for (const [lane, id] of Object.entries(wins)) {
    try {
      for (const t of await chrome.tabs.query({windowId: id}))
        out.push({tabId: t.id, url: t.url, title: t.title, active: t.active, opener: t.openerTabId, lane: Number(lane)});
    } catch (e) { /* fenêtre fermée */ }
  }
  return out;
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
      const init = initialTabs[lane];
      if (init && init !== t.id) { chrome.tabs.remove(init).catch(() => {}); initialTabs[lane] = null; }
      return {tabId: t.id};
    }
    case 'tabs.list': return await ourTabs();
    case 'tabs.close': await chrome.tabs.remove(m.tabId); return true;
    case 'tabs.activate': await chrome.tabs.update(m.tabId, {active: true}); return true;
    case 'window.show': {
      const w = await ensureWindow(m.lane || 0);
      await chrome.windows.update(w, {focused: true, state: 'normal'});
      return true;
    }
    case 'cdp': {
      await attach(m.tabId);
      // l'onglet passe au premier plan de SA fenêtre seulement pour la souris, le clavier et les captures
      if (/^Input\.|^Page\.captureScreenshot/.test(m.method)) {
        const t = await chrome.tabs.get(m.tabId).catch(() => null);
        if (t && !t.active) await chrome.tabs.update(m.tabId, {active: true}).catch(() => {});
      }
      return await chrome.debugger.sendCommand({tabId: m.tabId}, m.method, m.params || {});
    }
    default: throw new Error('commande inconnue ' + m.cmd);
  }
}

chrome.alarms.create('keepalive', {periodInMinutes: 0.4});
chrome.alarms.onAlarm.addListener(() => { if (!ws) connect(); else send({event: 'ping'}); });
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
