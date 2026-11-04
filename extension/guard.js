// StageAuto Bridge — garde des fenêtres StageAuto.
// D'autres extensions (gestionnaires de mots de passe comme Proton Pass, correcteurs…) insèrent leurs propres cadres
// chrome-extension:// dans les pages. Chrome interdit alors à StageAuto de piloter l'onglet (« Cannot access a
// chrome-extension:// URL of different extension »). Dans les fenêtres StageAuto UNIQUEMENT, ces cadres sont retirés
// dès leur apparition. Tes autres fenêtres Chrome ne sont pas touchées.
(async () => {
  let ours = false;
  try { ours = await chrome.runtime.sendMessage({q: 'isStageAutoTab'}); } catch (e) { return; }
  if (!ours) return;
  const mine = chrome.runtime.getURL('');
  const shadowOf = (e) => (chrome.dom && chrome.dom.openOrClosedShadowRoot) ? chrome.dom.openOrClosedShadowRoot(e) : e.shadowRoot;
  const foreign = (f) => /^chrome-extension:\/\//.test(f.src || '') && !(f.src || '').startsWith(mine);
  const observed = new WeakSet();
  const sweep = (root) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('iframe').forEach(f => { if (foreign(f)) f.remove(); });
    root.querySelectorAll('*').forEach(e => { const sr = shadowOf(e); if (sr) { watch(sr); sweep(sr); } });
  };
  const watch = (root) => {
    if (observed.has(root)) return;
    observed.add(root);
    new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => {
      if (n.nodeType !== 1) return;
      if (n.tagName === 'IFRAME' && foreign(n)) { n.remove(); return; }
      const sr = shadowOf(n); if (sr) { watch(sr); sweep(sr); }
      sweep(n);
    }))).observe(root, {childList: true, subtree: true});
  };
  watch(document);
  if (document.documentElement) sweep(document);
  else document.addEventListener('DOMContentLoaded', () => sweep(document));
  // les hôtes à racine fantôme fermée reçoivent parfois leur cadre un peu plus tard
  setTimeout(() => sweep(document), 1500);
  setTimeout(() => sweep(document), 5000);
})();
