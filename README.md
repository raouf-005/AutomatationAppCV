# StageAuto

Application Windows qui fait, deux fois par jour, la routine de candidature de stage d'Abderraouf Derardja
(M2 Sécurité et Réseaux, Université Paris-Saclay, stage de 6 mois dès février 2027) :

1. **Offres du jour** sur LinkedIn, JobTeaser, Welcome to the Jungle, Indeed, HelloWork, SmartRecruiters et orange.jobs.
2. **CV et lettre ciblés** pour chaque offre avec le skill `candidature-stage-ciblee` (gabarit v16, PDF d'une page contrôlé ATS).
3. **Candidature** envoyée par un agent Claude qui pilote **ton Chrome** comme Claude in Chrome : il lit la page, clique,
   remplit, joint le CV et la lettre, puis vérifie la confirmation.
4. **Contacts LinkedIn** : recruteur et manager de chaque offre. InMail si possible, sinon invitation avec note. Quand une
   personne accepte, il envoie un message avec le CV, la lettre et les certificats.
5. **Gmail** : e-mails demandant une action (entretien, test), confirmations d'adresse, réponses de recruteurs.
6. **Récapitulatif** dans le tableau de bord, en notification Windows et, si tu veux, sur ton téléphone (ntfy.sh).

## Installation (une fois)

1. Télécharge le dépôt (bouton **Code → Download ZIP**) et décompresse-le, par exemple dans `C:\StageAuto`.
   Décompresse ensuite **`StageAuto_personnel.zip`** (envoyé à part, jamais publié) à la racine : il crée le dossier
   `personal/`, qui contient ton profil, le skill CV et ton historique (voir `personal.example/README.md`).
2. Double-clique **`install.bat`**. Il installe Python, les dépendances, MiKTeX (LaTeX), qpdf et poppler. Il crée aussi
   `Downloads\Candidatures\envoyees` et les passages planifiés de **11h** et **19h**.
3. Dans Chrome, ouvre `chrome://extensions`, active le **Mode développeur**, puis clique **Charger l'extension non
   empaquetée** et choisis le dossier `extension`.
4. Double-clique **`StageAuto.bat`** : le tableau de bord s'ouvre (http://127.0.0.1:8765). L'icône de l'extension
   affiche « on » quand elle est connectée.
5. Dans **Réglages** :
   - colle ta **clé API Claude** (console.anthropic.com). Elle est stockée dans le Gestionnaire d'identification Windows ;
   - facultatif : **importe un export CSV Proton Pass**. Crée dans Proton Pass un coffre « Stage » qui ne contient que
     les sites d'emploi et exporte-le en CSV. L'application garde ces identifiants dans le Gestionnaire d'identification
     Windows, puis supprime le fichier.
6. Connecte-toi une fois dans ton Chrome à LinkedIn, JobTeaser (espace Université Paris-Saclay), Welcome to the Jungle,
   Indeed, HelloWork, orange.jobs et Gmail. Le bouton « Se connecter » du tableau de bord ouvre la bonne page.

## Utilisation

- Les passages se lancent seuls à 11h et 19h, ou avec **Lancer un passage**.
- Au passage de 19h, seules les offres apparues depuis le passage précédent sont traitées.
- S'il manque une connexion, une notification te le dit tout de suite. Les autres sites continuent, et les offres
  mises de côté sont réessayées dès que tu t'es connecté.
- **Mode validation** (réglage « Envoyer automatiquement » décoché) : l'agent remplit tout et s'arrête avant l'envoi.
  Tu valides ensuite chaque candidature d'un clic dans le tableau de bord.
- Les PDF envoyés sont copiés dans `Downloads\Candidatures\envoyees\<Entreprise_Sujet>\`.

## Mémoire des sites et courbe d'apprentissage

- Après chaque candidature réussie, l'application **mémorise la méthode du site** : boutons pour ouvrir le formulaire
  (« Candidature simplifiée »…), boutons d'étape (« Suivant », « Vérifier »…), bouton d'envoi et champs de texte.
  La clé est le site plus l'adresse du formulaire.
- Pour l'offre suivante sur ce site, elle **rejoue cette méthode sans IA** : remplissage automatique, étapes, envoi,
  puis vérification de la confirmation. À la première surprise (question inconnue, bouton absent, erreur affichée),
  l'agent reprend la main sur la page en l'état et complète la méthode.
- Les **réponses données par l'agent** aux questions des formulaires sont mémorisées et remplies d'office ensuite.
- L'onglet **Mémoire et apprentissage** montre, par site : le nombre de candidatures, le temps moyen des 5 premières puis
  des 5 dernières, le nombre d'étapes d'IA et la part rejouée sans IA. On y voit aussi les méthodes, les réponses apprises
  et les notes de l'agent.

## Fournisseur d'IA : Claude ou ta propre clé

Par défaut, l'application utilise Claude. Dans **Réglages → Fournisseur d'IA**, tu peux choisir un autre service
compatible OpenAI avec ta clé : OpenAI, Mistral, Google Gemini, DeepSeek, OpenRouter, Groq, ou une URL libre. Indique
le nom exact du modèle chez ce fournisseur. Avec « Si le fournisseur principal échoue, utiliser l'autre », l'autre
service prend le relais si une clé est refusée, si le crédit est épuisé ou si le service est indisponible. La qualité
des CV et de l'agent dépend du modèle choisi.

## LinkedIn : uniquement ton vrai Chrome, souris et clavier humains

- LinkedIn ne passe **que par ton Chrome habituel**, avec l'extension. Si elle n'est pas connectée, LinkedIn est
  ignoré : jamais de navigateur de secours pour LinkedIn.
- Sur LinkedIn, la souris suit des **trajectoires courbes** à vitesse variable et clique dans la zone centrale du bouton,
  pas au pixel près. Le clavier tape **caractère par caractère**, avec un rythme irrégulier et des pauses. La molette
  défile par crans.
- Un **curseur vert** visible dans la page montre où l'application agit, avec un cercle à chaque clic.
- **Tester LinkedIn (sans rien envoyer)** (carte Connexions) : connexion, recherche des offres, lecture d'une offre,
  remplissage de la Candidature simplifiée sous tes yeux, puis arrêt avant « Envoyer la candidature ».
- Réglages : « Souris et clavier humains » (LinkedIn / partout / jamais) et « Afficher le curseur ».

## Comment ça marche

| Étape | Méthode | Coût Claude |
|---|---|---|
| Connexions, recherche, lecture des offres | scripts dans tes onglets (pas d'IA) | 0 |
| Éligibilité + CV + lettre | 1 appel par offre, skill `candidature-stage-ciblee`, contrôle `build.py`, 1 correction si besoin | moyen |
| Formulaires | agent navigateur : `auto_fill` remplit d'un coup les champs connus, puis l'agent gère le reste | faible (effort `low`) |
| Contacts LinkedIn, suivi des acceptations | agent navigateur avec les textes exacts fournis | faible |

- **Ton Chrome, tes sessions** : l'extension *StageAuto Bridge* relaie les commandes du protocole Chrome DevTools
  (`chrome.debugger`), comme Claude in Chrome. Les clics et la saisie sont de vrais événements souris et clavier, et les
  fichiers sont joints directement dans le champ. L'application travaille dans une fenêtre Chrome à part ; Chrome affiche
  une barre « StageAuto Bridge débogue ce navigateur » pendant ce temps.
- **Repli** : réglage « Chrome dédié ». L'application lance alors son propre Chrome (profil `%LOCALAPPDATA%\StageAuto`),
  dans lequel il faut se connecter une fois.
- **Apprentissage** : après chaque candidature, l'agent note la méthode la plus rapide pour le site (onglet
  « Méthodes apprises »). Ces notes sont réutilisées au passage suivant, avec les réponses déjà données aux questions
  des formulaires (base `answers`) et les sites carrières qui exigent un compte (sautés ensuite).
- **Données personnelles** : profil, photo, skill et historique restent dans `personal/`, exclu du dépôt public.
- **Historique** : `seen.txt` et `contacts.md` du kit précédent sont importés : aucune offre ni personne n'est traitée deux fois.

## Règles respectées (codées, pas seulement demandées)

- Réponses personnelles limitées au profil autorisé (`stageauto/config.py`, `PROFILE`). Une question sur le titre de
  séjour, le casier, une pièce d'identité, la banque, la date de naissance ou une vidéo obligatoire fait abandonner l'offre.
- Offre exigeant la nationalité française, la citoyenneté UE ou une habilitation : ignorée.
- Jamais de création de compte. Les mots de passe ne passent jamais par Claude : l'outil `fill_credentials` les saisit
  sans les montrer. Les captchas ne sont jamais résolus : l'offre attend que tu te connectes.
- Au plus 3 offres par entreprise et par jour, 20 invitations LinkedIn et 5 InMails par jour (réglables).
- CV et lettre : uniquement les faits et chiffres du profil du skill, sans outil interdit.

## À savoir

- **Sites réels non testés** : les sélecteurs de recherche et les sites d'emploi n'ont pas pu être testés depuis
  l'environnement de développement (pas d'accès réseau à ces sites). L'agent s'adapte aux pages, mais les premiers
  passages sont à surveiller dans l'onglet *Journal*.
- **LinkedIn** interdit l'automatisation dans ses conditions d'utilisation. Les limites basses réduisent le risque de
  restriction du compte, sans le supprimer.
- **Coût** : compter grossièrement quelques dizaines de centimes de dollar par candidature (CV + agent). La facture
  Anthropic fait foi.
- **Extension et pièces jointes** : si Chrome refuse de joindre un fichier, active « Autoriser l'accès aux URL de
  fichier » dans les détails de l'extension. L'application bascule aussi d'elle-même sur une méthode de secours.

## Structure

```
extension/            extension Chrome StageAuto Bridge (MV3, chrome.debugger)
stageauto/
  cdp.py              couche navigateur (extension ou Playwright), souris/clavier humains, curseur visible, instantané de page
  providers.py        fournisseurs d'IA : Claude ou service compatible OpenAI (clé personnelle), relais automatique
  replay.py           relecture des méthodes mémorisées sans IA
  agent.py            agent Claude (outils read_page, find, click, type, upload, auto_fill, fill_credentials, finish…)
  forms.py            réponses autorisées, remplissage automatique, détection des questions interdites
  cvskill.py          CV + lettre via le skill (SKILL.md + profil.md), build.py, correction, repli modèles
  sites.py            connexion, recherche et lecture des offres par site
  linkedin_contacts.py, gmail.py, pipeline.py, server.py, static/index.html
personal/             NON PUBLIÉ (.gitignore) : profile.json, skill/candidature-stage-ciblee, tpl/, seed/
personal.example/     structure attendue du dossier personnel
tests/                test hors ligne (formulaire local, agent simulé)
install.bat, StageAuto.bat, requirements.txt
```

Ligne de commande : `StageAuto.bat` (tableau de bord), `StageAuto.bat --run` (passage immédiat),
`.venv\Scripts\python -m stageauto.selftest` (vérifie LaTeX et les outils PDF).
