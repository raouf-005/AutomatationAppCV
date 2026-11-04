"""Agent navigateur Claude, sur le modèle de Claude in Chrome : il lit la page (éléments numérotés), agit par
références (clic souris réel, saisie clavier, envoi de fichier), et apprend une méthode par site.

Sécurité : les mots de passe ne passent jamais par Claude (outil fill_credentials) ; les chemins de fichiers non plus
(upload prend « cv », « lettre » ou « certificats »).
"""
import asyncio
import base64
import json
import re
import time

from . import db, forms, providers, vault
from .cdp import BrowserError, Tab
from .config import PROFILE, load_settings
from .notify import log

FINAL_RE = re.compile(r"^(envoyer|soumettre|submit|postuler|valider|send|déposer|confirmer)(\b| )(ma |la |votre |your |my )?"
                      r"(candidature|application)?\s*$", re.I)


def T(name, desc, props, req=None):
    return {"name": name, "description": desc, "strict": True,
            "input_schema": {"type": "object", "properties": props, "required": req if req is not None else list(props),
                             "additionalProperties": False}}


S = {"type": "string"}
TOOLS = [
    T("read_page", "Liste les éléments interactifs visibles (ref, rôle, nom, valeur) + un extrait du texte. "
      "Si un dialogue modal est ouvert, seul son contenu est listé (scope='page' pour toute la page).",
      {"scope": {"type": "string", "enum": ["auto", "page"]}}),
    T("find", "Cherche des éléments ou du texte par mots (ex. 'bouton postuler', 'lettre de motivation'). Plus rapide qu'une capture.",
      {"query": S}),
    T("click", "Clic souris réel sur un élément (ref issue du dernier read_page/find/auto_fill).", {"ref": S}),
    T("type", "Saisit du texte dans un champ (remplace le contenu). Jetons spéciaux : @@LETTRE@@ (lettre complète en texte), "
      "@@MESSAGE@@ (message court < 400 car.).", {"ref": S, "text": S, "submit": {"type": "boolean"}}),
    T("select", "Choisit une option d'une liste <select> native par son libellé.", {"ref": S, "option": S}),
    T("check", "Coche ou décoche une case / un bouton radio.", {"ref": S, "checked": {"type": "boolean"}}),
    T("upload", "Envoie un fichier dans le champ fichier (ou le champ fichier le plus proche du bouton indiqué).",
      {"ref": S, "file": {"type": "string", "enum": ["cv", "lettre", "certificats"]}}),
    T("auto_fill", "Remplit d'un coup tous les champs reconnus de l'étape visible avec les réponses autorisées "
      "(identité, contact, CV, lettre, consentements…). À utiliser EN PREMIER sur chaque étape de formulaire.", {}),
    T("fill_credentials", "Saisit l'identifiant et le mot de passe enregistrés pour ce site (tu ne les vois jamais).",
      {"user_ref": S, "password_ref": S}),
    T("press", "Touche clavier.", {"key": {"type": "string", "enum": ["Enter", "Tab", "Escape", "ArrowDown", "ArrowUp", "Backspace", "Space"]}}),
    T("scroll", "Fait défiler la page ou le dialogue.", {"direction": {"type": "string", "enum": ["down", "up"]}}),
    T("navigate", "Ouvre une URL dans l'onglet courant.", {"url": S}),
    T("tabs", "Liste les onglets, passe à un onglet (index) ou ferme l'onglet courant.",
      {"action": {"type": "string", "enum": ["list", "switch", "close"]}, "index": {"type": "integer"}}),
    T("screenshot", "Capture de l'écran à demi-résolution. Seulement si la liste d'éléments ne suffit pas.", {}),
    T("wait", "Attend quelques secondes (chargement).", {"seconds": {"type": "integer"}}),
    T("finish", "Termine la tâche. status : applied (candidature envoyée, confirmation vue), ready (tout rempli, en attente "
      "de validation), already_applied, blocked (impossible / interdit), needs_login (connexion ou captcha requis), "
      "not_eligible, done (tâche non candidature terminée), replied (la personne a répondu). method_note : 1-3 phrases "
      "sur la méthode la plus rapide pour ce site (URL, boutons, pièges) pour la prochaine fois.",
      {"status": {"type": "string", "enum": ["applied", "ready", "already_applied", "blocked", "needs_login", "not_eligible",
                                             "done", "replied"]},
       "reason": S, "method_note": S,
       "contacts": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                               "required": ["name", "role", "profile_url", "action"],
                                               "properties": {"name": S, "role": S, "profile_url": S,
                                                              "action": {"type": "string", "enum": ["invite", "inmail", "message", "none"]}}}}}),
]

SYSTEM = f"""Tu es l'agent de candidature d'Abderraouf Derardja (M2 Sécurité et Réseaux, Université Paris-Saclay), qui cherche un
stage de fin d'études de 6 mois à partir de février 2027. Tu pilotes son navigateur Chrome (sessions déjà ouvertes) avec les
outils fournis, comme Claude in Chrome : read_page/find donnent des références d'éléments (e12…), tu agis par référence.

MÉTHODE (rapidité) :
- Sur chaque étape de formulaire : auto_fill d'abord, puis seulement les champs restants. find plutôt que screenshot.
- Les références changent après chaque read_page/find/auto_fill : utilise toujours les plus récentes.
- Plusieurs actions indépendantes peuvent être envoyées dans le même tour ; elles s'exécutent dans l'ordre.
- Après un clic qui ouvre un nouvel onglet, le résultat le signale : tu es alors sur ce nouvel onglet.
- Cookies non essentiels : refuser. Newsletters, alertes, « suivre l'entreprise » : décocher.

RÉPONSES AUTORISÉES (n'utilise rien d'autre) : {json.dumps(PROFILE, ensure_ascii=False)}
Source de l'offre : le site où elle a été trouvée, sinon « Autre ». Visa sponsorship : Non. Prêt à déménager : Oui.
Années d'expérience : estimation honnête et prudente (1). Prétentions : « Selon la grille de l'entreprise ».
Date de début du stage différente de février 2027 (2026, dès que possible…) : ce n'est PAS un motif de refus ; postule
quand même et, si on te demande ta date de disponibilité, donne celle du profil.
Formation (établissement, diplôme, domaine, année de début « De », année de fin « À ») : la liste etudes, exactement comme
sur le CV ; ce ne sont pas des données sensibles. Moyenne, notes, mention : laisser vide si facultatif, sinon finish blocked.

INTERDITS (finish blocked, sans contourner) :
- créer un compte, inventer ou saisir un mot de passe (seul fill_credentials le peut), données bancaires, pièce d'identité ;
- titre de séjour, autorisation de travail, casier, n° de sécurité sociale, date de naissance, ou toute donnée sensible absente
  des réponses autorisées ; nationalité française, citoyenneté UE ou habilitation exigées ; vidéo obligatoire ;
- résoudre un captcha : finish needs_login (il se connectera lui-même) ; page de connexion sans identifiants enregistrés :
  finish needs_login ;
- postuler deux fois : si la page indique « Candidature envoyée », « Vous avez postulé », « Suivi de candidature » -> finish already_applied.
Ne réponds jamais à une question par une compétence ou un fait absent du profil. Le succès d'une candidature se constate
(message « candidature envoyée / merci », page de confirmation) avant finish applied."""

_PROFILE_AT_IMPORT = json.dumps(PROFILE, ensure_ascii=False)


def system() -> str:
    """Consignes avec le profil actuel (modifiable dans l'onglet « Mon profil » sans redémarrer)."""
    return SYSTEM.replace(_PROFILE_AT_IMPORT, json.dumps(PROFILE, ensure_ascii=False))


SITE_HINTS = {
    "linkedin": "LinkedIn Candidature simplifiée : bouton « Candidature simplifiée » ; dans le dialogue : Suivant, CV "
                "(« Importer le CV » puis upload cv ; la radio du nouveau CV est cochée d'office), lettre si demandée, questions, "
                "« Vérifier », « Envoyer la candidature » ; succès = « Candidature envoyée ». Sinon « Postuler » ouvre le site "
                "de l'entreprise dans un nouvel onglet.",
    "hellowork": "HelloWork : formulaire direct en bas de page (#postuler) : textarea motivation (@@LETTRE@@), « Utiliser un "
                 "nouveau CV » (upload cv), bouton « Postuler » ; succès = URL /bounce/. Variante en 2 étapes « Continuer ma "
                 "candidature ». Sinon « site du recruteur » (souvent compte requis).",
    "wttj": "Welcome to the Jungle : bouton « Postuler » : soit dialogue WTTJ (CV, lettre, consentement), soit site externe.",
    "jobteaser": "JobTeaser : « Postuler » ouvre d'abord une fenêtre « profil à jour » (Continuer), puis le formulaire JobTeaser ou "
                 "l'ATS externe dans un nouvel onglet.",
    "indeed": "Indeed : « Postuler maintenant » = formulaire Indeed en plusieurs étapes (Continuer…, Déposer ma candidature). "
              "« Postuler sur le site de l'entreprise » = site externe. Vérification Cloudflare -> finish needs_login.",
    "smartrecruiters": "SmartRecruiters : « Je suis intéressé(e) » ; déposer le CV (upload cv) qui préremplit le formulaire ; "
                       "Ville = « Paris, Île-de-France, France » ; message SANS point-virgule ; case confidentialité ; Envoyer.",
    "orange": "orange.jobs : formulaire /fr/fr/apply?jobSeqNo=… (CV seul, Civilité = M, Lieu) ; cocher le consentement en "
              "DERNIER puis Soumettre ; la page peut geler ~45 s : ne pas resoumettre, attendre applythankyou?status=success.",
}


def clean_name(name: str) -> str:
    """Nom de bouton réutilisable d'une offre à l'autre (« Candidature simplifiée pour Stage X » -> « Candidature simplifiée »)."""
    n = re.split(r"\s+(pour|for|à|to)\s+|\s+[–-]\s+", name or "", maxsplit=1)[0]
    return n.strip()[:60]


class Agent:
    def __init__(self, browser, tab: Tab, site: str, files: dict | None = None, offer: dict | None = None):
        self.browser, self.tab, self.site = browser, tab, site
        self.files = files or {}
        self.offer = offer or {}
        self.auto = load_settings().get("auto_submit", True)
        self.result = None
        self.steps = 0
        self.refs: dict = {}
        self.trace: list = []
        self.recipe_key = None   # clé de mémoire du site (site|hôte) ; fixée par l'appelant pour les candidatures
        self.extra_intro = ""
        self._warned = None
        self.checked_before_submit = False

    # --------------------------------------------------------------------------- rendu d'une page
    @staticmethod
    def render(snap, limit=9000):
        out = [f"URL : {snap['url']}", f"Titre : {snap['title']}"]
        if snap.get("dialog"):
            out.append("[dialogue modal ouvert : seul son contenu est listé]")
        if snap.get("captcha"):
            out.append("[CAPTCHA / vérification détectée]")
        if snap.get("errors"):
            out.append("Erreurs affichées : " + " | ".join(snap["errors"]))
        out.append(f"Éléments ({len(snap['items'])}{'+' if snap.get('truncated') else ''}) :")
        for it in snap["items"]:
            s = f"{it['ref']} {it['role']} \"{it['name']}\""
            if it.get("value"):
                s += f" = \"{it['value']}\""
            if "checked" in it:
                s += " [x]" if it["checked"] else " [ ]"
            if it.get("group"):
                s += f" (question : {it['group'][:80]})"
            if it.get("options"):
                s += " options[" + "|".join(it["options"][:15]) + "]"
            if it.get("files"):
                s += " (fichier déjà joint)"
            if it.get("required"):
                s += " *requis"
            if it.get("invalid"):
                s += " !invalide"
            if it.get("href") and it["role"] == "link":
                s += f" -> {it['href'][:70]}"
            out.append(s)
        txt = "\n".join(out)
        if len(txt) > limit:
            txt = txt[:limit] + "\n… (liste tronquée : utilise find)"
        return txt + "\n\nTexte (extrait) :\n" + (snap.get("text") or "")[:1800]

    def _view(self, snap, limit=9000):
        self.refs = {it["ref"]: it for it in snap["items"]}
        return self.render(snap, limit)

    def _remember(self, label, options, value, section=""):
        """Réponse donnée par l'agent : mémorisée pour que le remplissage automatique la reprenne la fois suivante.
        Pas dans un bloc Formation / Expérience : « De », « À », « Établissement » y changent de sens d'une entrée à l'autre."""
        if re.search(forms.EDU_SECTION_RE, forms.nrm(section or "")):
            return
        if label and value and len(label) > 2 and not forms.BLOCK_RE.search(label):
            db.put_answer(label, options or None, value, source="agent")

    # --------------------------------------------------------------------------------- outils
    async def run_tool(self, name, a):
        tab = self.tab
        if name == "read_page":
            await tab._cursor(*tab.pos, label="Lecture de la page")
            return self._view(await tab.snapshot(page=a.get("scope") == "page"))
        if name == "find":
            await tab._cursor(*tab.pos, label=f"Recherche : {a['query'][:30]}")
            snap = await tab.snapshot(page=True, max=400, textMax=20000)
            self.refs = {it["ref"]: it for it in snap["items"]}
            words = [w for w in forms.nrm(a["query"]).split() if len(w) > 2]
            hits = [it for it in snap["items"] if any(w in forms.nrm(it["name"] + " " + it.get("href", "")) for w in words)]
            lines = [f"{it['ref']} {it['role']} \"{it['name']}\"" + (f" = \"{it.get('value')}\"" if it.get("value") else "")
                     for it in hits[:25]]
            t = snap.get("text") or ""
            ctx = []
            for w in words[:3]:
                for m in re.finditer(re.escape(w), forms.nrm(t)):
                    ctx.append("…" + t[max(0, m.start() - 80): m.start() + 120].replace("\n", " ") + "…")
                    if len(ctx) >= 4:
                        break
            return ("Éléments :\n" + ("\n".join(lines) or "aucun") + "\nTexte :\n" + ("\n".join(ctx) or "aucun"))[:5000]
        if name == "click":
            item = self.refs.get(a["ref"], {})
            label = (item.get("name") or "").strip()
            if not self.auto:
                it = await tab.eval("(r) => { const e = (window.__sa||{map:{}}).map[r]; if (!e) return null;"
                                    " const f = e.closest('form, [role=dialog]');"
                                    " return {t: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim(), form: !!f} }", a["ref"])
                if it and it["form"] and FINAL_RE.match(it["t"]):
                    return "MODE VALIDATION : ne clique pas sur l'envoi final. Appelle finish(status='ready')."
            if self.auto and item.get("role") == "button" and FINAL_RE.match(label):
                # contrôle avant l'envoi, sans renuméroter les références
                probs = forms.presubmit_problems(await tab.snapshot(keep=True))
                if probs and probs != self._warned:
                    self._warned = probs
                    return ("VÉRIFICATION AVANT ENVOI — à corriger d'abord (auto_fill, puis les champs restants) : "
                            + " ; ".join(probs) + ". Si tu es sûr que c'est une fausse alerte, reclique sur le bouton d'envoi.")
                self.checked_before_submit = True
            before = set(await self.browser.tab_ids())
            await tab.click(a["ref"])
            await asyncio.sleep(0.25)
            newt = await self.browser.wait_new_tab(before, timeout=1.0, opener=tab.id)
            if item.get("role") in ("button", "link") and label:
                self.trace.append({"a": "click", "name": clean_name(label), "final": bool(FINAL_RE.match(label)),
                                   "new_tab": bool(newt)})
            if item.get("role") == "radio" and item.get("group"):
                opts = [i["name"] for i in self.refs.values() if i.get("group") == item["group"]]
                self._remember(item["group"], opts, label)
            if newt:
                self.tab = newt
                return "Nouvel onglet ouvert, tu es dessus maintenant.\n" + self._view(await newt.snapshot())
            await tab.settle(8)
            return "OK\n" + self._view(await tab.snapshot(), limit=6000)
        if name == "type":
            item = self.refs.get(a["ref"], {})
            token = next((t for t in ("@@LETTRE@@", "@@MESSAGE@@") if t in a["text"]), None)
            text = a["text"].replace("@@LETTRE@@", self.files.get("lettre_txt", "")).replace(
                "@@MESSAGE@@", self.files.get("message_court", ""))
            if self.files.get("no_semicolon"):
                text = text.replace(";", ",")
            await tab.type(a["ref"], text, submit=a.get("submit", False))
            if token:
                self.trace.append({"a": "type", "name": item.get("name", ""), "text": token})
            elif item.get("role", "").startswith(("textbox", "textarea", "combobox")) and len(text) < 300:
                self._remember(item.get("name"), None, text, item.get("section", ""))
            return "OK"
        if name == "select":
            item = self.refs.get(a["ref"], {})
            r = await tab.select(a["ref"], a["option"])
            if r and r.get("selected"):
                self._remember(item.get("name"), item.get("options"), r["selected"], item.get("section", ""))
            return json.dumps(r, ensure_ascii=False)
        if name == "check":
            item = self.refs.get(a["ref"], {})
            await tab.check(a["ref"], a["checked"])
            if item.get("role") == "radio" and a["checked"] and item.get("group"):
                opts = [i["name"] for i in self.refs.values() if i.get("group") == item["group"]]
                self._remember(item["group"], opts, item.get("name"))
            return "OK"
        if name == "upload":
            item = self.refs.get(a["ref"], {})
            key = {"cv": "cv", "lettre": "lm", "certificats": "certs"}[a["file"]]
            path = self.files.get(key)
            if not path:
                return f"fichier {a['file']} indisponible"
            await tab.upload(a["ref"], path)
            self.trace.append({"a": "upload", "file": a["file"], "name": clean_name(item.get("name", ""))})
            return f"fichier {a['file']} envoyé"
        if name == "auto_fill":
            ctx = {"cv": self.files.get("cv"), "lm": self.files.get("lm"), "certs": self.files.get("certs"),
                   "lettre_txt": self.files.get("lettre_txt", ""), "source": self.offer.get("source", "Autre"),
                   "no_semicolon": self.files.get("no_semicolon"), "region": self.offer.get("region")}
            try:
                rep = await forms.auto_fill(tab, ctx, self.offer)
            except forms.NeedLogin:
                return "Un mot de passe est demandé : utilise fill_credentials si des identifiants existent, sinon finish needs_login."
            self.trace.append({"a": "auto_fill"})
            return (f"Rempli : {rep['filled'] or 'rien'}\nObligatoires sans réponse : {rep['unanswered_required'] or 'aucun'}\n"
                    f"Ignorés : {rep['skipped'] or 'aucun'}\n\n" + self._view(await tab.snapshot(), limit=6000))
        if name == "fill_credentials":
            url = await tab.url()
            host = re.sub(r"^https?://([^/]+).*", r"\1", url).lower()
            cred = vault.get_cred(self.site) or vault.get_cred("host:" + host)
            if not cred:
                for s, hs in vault.SITE_HOSTS.items():
                    if any(host.endswith(h) for h in hs):
                        cred = vault.get_cred(s)
            if not cred:
                return "Aucun identifiant enregistré pour ce site : finish needs_login."
            await tab.type(a["user_ref"], cred["user"])
            await tab.type(a["password_ref"], cred["password"])
            return "Identifiants saisis (non affichés). Clique maintenant sur le bouton de connexion."
        if name == "press":
            await tab.press(a["key"])
            await asyncio.sleep(0.6)
            return "OK"
        if name == "scroll":
            await tab.scroll(700 if a["direction"] == "down" else -700)
            return "OK"
        if name == "navigate":
            await tab.goto(a["url"])
            return self._view(await tab.snapshot(), limit=6000)
        if name == "tabs":
            ids = await self.browser.tab_ids()
            if a["action"] == "list":
                return "\n".join(f"{i}: onglet {t}{' (courant)' if t == tab.id else ''}" for i, t in enumerate(ids))
            if a["action"] == "switch":
                if 0 <= a.get("index", -1) < len(ids):
                    self.tab = Tab(self.browser, ids[a["index"]])
                    return self._view(await self.tab.snapshot(), limit=6000)
                return "index invalide"
            if a["action"] == "close":
                await tab.close()
                ids = await self.browser.tab_ids()
                self.tab = Tab(self.browser, ids[-1]) if ids else None
                return "onglet fermé"
        if name == "screenshot":
            img = await tab.screenshot()
            if not img:
                return "capture impossible"
            return [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.b64encode(img).decode()}}]
        if name == "wait":
            await asyncio.sleep(min(max(a.get("seconds", 2), 1), 15))
            return "OK"
        if name == "finish":
            self.result = a
            return "terminé"
        return "outil inconnu"

    # ------------------------------------------------------------------------- mémoire du site
    def _save_recipe(self):
        """Transforme les actions réussies en méthode rejouable : boutons d'ouverture, d'étape, d'envoi."""
        key = self.recipe_key
        if not key or not self.trace:
            return
        first_form = next((i for i, t in enumerate(self.trace) if t["a"] in ("auto_fill", "upload", "type")), len(self.trace))
        opn = [t["name"] for t in self.trace[:first_form] if t["a"] == "click" and not t["final"]]
        nxt = [t["name"] for t in self.trace[first_form:] if t["a"] == "click" and not t["final"]]
        sub = [t["name"] for t in self.trace if t["a"] == "click" and t["final"]]
        tokens = [[t["name"], t["text"]] for t in self.trace if t["a"] == "type"]
        uploads = [[t["name"], t["file"]] for t in self.trace if t["a"] == "upload"]
        m = db.method(key)
        r = m.get("recipe") or {"open": [], "next": [], "submit": [], "tokens": [], "uploads": [], "ok": 0}

        def merge(old, new):
            out = list(old)
            for x in new:
                if x and x not in out:
                    out.append(x)
            return out[:8]
        r["open"] = merge(opn, r["open"])
        r["next"] = merge(r["next"], nxt)
        r["submit"] = merge(r["submit"], sub)
        r["tokens"] = merge(r["tokens"], tokens)
        r["uploads"] = merge(r["uploads"], uploads)
        r["ok"] = r.get("ok", 0) + 1
        r["updated"] = db.now()
        m["recipe"] = r
        db.save_method(key, m)
        log(f"méthode mémorisée pour {key} : ouvrir {r['open']}, étapes {r['next']}, envoi {r['submit']}")

    # ----------------------------------------------------------------------------- boucle
    async def run(self, goal: str, max_steps: int | None = None) -> dict:
        s = load_settings()
        max_steps = max_steps or s.get("agent_max_steps", 40)
        notes = db.method(self.site).get("notes", [])
        hint = SITE_HINTS.get(self.site, "")
        recipe = db.method(self.recipe_key).get("recipe") if self.recipe_key else None
        first = await self.tab.snapshot()
        if self.recipe_key and self.offer.get("_start_url") is None:
            self.offer["_start_url"] = first.get("url")
        intro = (f"TÂCHE : {goal}\n\n"
                 + (f"Méthode connue pour ce site : {hint}\n" if hint else "")
                 + (f"Méthode mémorisée (boutons) : ouvrir {recipe['open']}, étapes {recipe['next']}, envoi {recipe['submit']}\n"
                    if recipe else "")
                 + ("Notes des passages précédents :\n- " + "\n- ".join(notes[-6:]) + "\n" if notes else "")
                 + (self.extra_intro + "\n" if self.extra_intro else "")
                 + ("MODE VALIDATION : remplis tout mais ne clique PAS l'envoi final ; appelle finish(status='ready').\n"
                    if not self.auto else "")
                 + "\nPage actuelle :\n" + self._view(first))
        conv = providers.ToolConversation(system(), TOOLS, effort=s.get("agent_effort", "low"))
        conv.user(intro)
        t0 = time.time()
        nudged = False
        errors = 0
        while self.steps < max_steps and self.result is None:
            self.steps += 1
            try:
                st = await asyncio.to_thread(conv.step)
            except Exception as e:
                errors += 1
                log(f"IA ({conv.provider}) : {e.__class__.__name__} {str(e)[:120]}")
                if providers.is_limit_error(e):  # limite atteinte : l'offre est rendue, reprise plus tard ou par une autre IA
                    return {"status": "wait_ai", "reason": f"limite d'IA : {str(e)[:140]}", "steps": self.steps}
                if errors >= 3 or "refusée" in str(e) or e.__class__.__name__ == "AuthenticationError":
                    return {"status": "wait_ai", "reason": f"fournisseur d'IA : {e.__class__.__name__} {str(e)[:100]}",
                            "steps": self.steps}
                await asyncio.sleep(5)
                continue
            if st.refusal:
                return {"status": "blocked", "reason": "requête déclinée par le modèle", "steps": self.steps}
            if not st.calls:
                if nudged:
                    break
                nudged = True
                conv.user("Continue avec les outils, puis appelle finish.")
                continue
            results = []
            for c in st.calls:
                try:
                    out = await self.run_tool(c.name, c.input)
                    err = False
                except (BrowserError, forms.Blocked) as e:
                    out, err = f"Erreur : {e}", True
                except Exception as e:
                    out, err = f"Erreur : {e.__class__.__name__} {e}"[:300], True
                results.append({"id": c.id, "name": c.name, "content": out, "is_error": err})
                if c.name == "finish":
                    break
            done = {r["id"] for r in results}
            results += [{"id": c.id, "name": c.name, "content": "ignoré (tâche terminée)"} for c in st.calls if c.id not in done]
            conv.results(results)
        res = self.result or {"status": "blocked", "reason": f"pas terminé en {self.steps} étapes", "method_note": ""}
        res["steps"] = self.steps
        res["seconds"] = round(time.time() - t0)
        res["provider"] = conv.provider
        if res["status"] in ("applied",) or (res["status"] == "ready" and not self.auto):
            self._save_recipe()
        note = (res.get("method_note") or "").strip()
        if note:
            m = db.method(self.site)
            lst = [n for n in m.get("notes", []) if n != note]
            lst.append(f"{db.today()} : {note[:300]}")
            m["notes"] = lst[-10:]
            db.save_method(self.site, m)
        db.learn(self.site, duration=res["seconds"])
        log(f"agent {self.site} : {res['status']} en {self.steps} étapes, {res['seconds']} s — {res.get('reason', '')[:120]}")
        return res
