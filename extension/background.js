// StageAuto Bridge : relaie les commandes Chrome DevTools de l'application locale vers les onglets
// de la fenêtre StageAuto (même principe que Claude in Chrome : chrome.debugger + sessions de l'utilisateur).
const URL_WS = 'ws://127.0.0.1:8765/ext';
let ws = null;
let winId = null;
let initialTab = null;  // onglet vide créé avec la fenêtre StageAuto
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

async function ensureWindow() {
  if (winId === null) {
    const s = await chrome.storage.session.get('winId');
    if (s.winId) winId = s.winId;
  }
  if (winId !== null) { try { await chrome.windows.get(winId); return winId; } catch (e) { winId = null; } }
  const w = await chrome.windows.create({url: 'about:blank', focused: false, width: 1400, height: 900});
  winId = w.id;
  initialTab = w.tabs && w.tabs[0] ? w.tabs[0].id : null;
  await chrome.storage.session.set({winId});
  return winId;
}

async function attach(tabId) {
  if (attached.has(tabId)) return;
  try { await chrome.debugger.attach({tabId}, '1.3'); }
  catch (e) { if (!String(e).includes('already attached')) throw e; }
  attached.add(tabId);
}

chrome.debugger.onDetach.addListener((src) => attached.delete(src.tabId));
chrome.tabs.onRemoved.addListener((id) => { attached.delete(id); send({event: 'tab_removed', tabId: id}); });
chrome.tabs.onCreated.addListener((t) => send({event: 'tab_created', tabId: t.id, opener: t.openerTabId, windowId: t.windowId}));

async function handle(m) {
  switch (m.cmd) {
    case 'ping': return 'pong';
    case 'tabs.create': {
      const w = await ensureWindow();
      const t = await chrome.tabs.create({windowId: w, url: m.url || 'about:blank', active: true});
      if (initialTab !== null && initialTab !== t.id) { chrome.tabs.remove(initialTab).catch(() => {}); initialTab = null; }
      return {tabId: t.id};
    }
    case 'tabs.list': {
      const w = await ensureWindow();
      return (await chrome.tabs.query({windowId: w})).map(t => ({tabId: t.id, url: t.url, title: t.title, active: t.active, opener: t.openerTabId}));
    }
    case 'tabs.close': await chrome.tabs.remove(m.tabId); return true;
    case 'tabs.activate': await chrome.tabs.update(m.tabId, {active: true}); return true;
    case 'window.show': { const w = await ensureWindow(); await chrome.windows.update(w, {focused: true, state: 'normal'}); return true; }
    case 'cdp': {
      await chrome.tabs.update(m.tabId, {active: true}).catch(() => {});
      await attach(m.tabId);
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
