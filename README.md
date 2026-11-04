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
   `Downloads\Candidatures\envoyees`, les passages planifiés de **11h** et **19h** et le démarrage discret à l'ouverture
   de session pour la veille.
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

- **Veille (postuler parmi les premiers)** : de 8h à 22h, toutes les ~30 min, l'application cherche les nouvelles offres
  et postule aussitôt. LinkedIn est consulté au plus une fois par heure, avec une seule recherche. `install.bat` lance
  l'application discrètement à l'ouverture de session pour que la veille tourne toute la journée.
- **Passages complets** à 11h et 19h : en plus des offres, contacts LinkedIn, suivi des acceptations, Gmail et nouvel essai
  des sites reconnectés.
- Boutons du tableau de bord : **Vérifier maintenant** (veille immédiate) et **Passage complet**.
- S'il manque une connexion, une notification te le dit. Les autres sites continuent, et les offres mises de côté sont
  réessayées dès que tu t'es connecté.
- **Mode validation** (réglage « Envoyer automatiquement » décoché) : l'agent remplit tout et s'arrête avant l'envoi.
  Tu valides ensuite chaque candidature d'un clic dans le tableau de bord.
- Les PDF envoyés sont copiés dans `Downloads\Candidatures\envoyees\<Entreprise_Sujet>\`.

## Postuler vite et parmi les premiers

- **Priorité** : chaque offre reçoit une note. Elle monte avec la récence (« il y a 12 minutes »), avec les signaux
  « Soyez parmi les premiers candidats » et « Recrutement actif », quand il y a peu de candidats, en candidature simplifiée,
  et quand le site a déjà une méthode mémorisée. Les CV des offres les mieux notées sont générés et envoyés en premier.
- **Flux continu** : chaque candidature part **dès que son CV est prêt**, sans attendre les autres. Pendant ce temps, les
  CV suivants sont générés en parallèle.
- **Deux voies en parallèle** : LinkedIn (souris humaine, plus lente) et les autres sites postulent en même temps, chacun
  dans sa fenêtre Chrome.
- **Pré-tri sans IA** : l'alternance, le CDI, les stages de moins de 6 mois, une habilitation ou la nationalité française
  exigée et un début en 2026 sont écartés avant tout appel à l'IA. En cas de doute, l'offre passe et l'IA tranche.
- **Attentes intelligentes** : l'application attend que la page soit prête au lieu de faire des pauses fixes, et repère
  tout de suite les nouveaux onglets.
- **CV rapide** (réglage) : modèles adaptés au lieu du skill complet, environ 4 fois plus rapide. La qualité maximale reste
  le skill, réglage par défaut.
- L'onglet **Mémoire et apprentissage** affiche le délai moyen entre la détection et la candidature, et l'âge des offres
  au moment de postuler.

## Ne rater aucune offre du jour

- **Tous les mots-clés** à chaque passage complet ; la veille (toutes les 30 min) en vérifie 3 qui tournent d'une veille à
  l'autre, pour que chacun soit revu plusieurs fois par jour.
- **Pages suivantes** : les recherches triées par date et limitées à la période lisent les pages de résultats suivantes
  tant qu'elles apportent des offres nouvelles (4 pages au plus, réglable).
- **Alertes e-mail** : les alertes des sites d'emploi reçues dans Gmail (LinkedIn, Indeed, HelloWork, Welcome to the
  Jungle, JobTeaser, APEC…) sont ouvertes, et leurs liens deviennent des offres. Crée des alertes « stage cybersécurité »
  sur ces sites : ce qu'une recherche laisse passer arrive par e-mail. Les liens de suivi sont suivis jusqu'à l'offre.
- **Rattrapage du soir** (22:30, réglable) : un passage complet relit toutes les sources sur les 26 dernières heures.
- **Sites carrières des entreprises** (voir plus bas) et **autres sites d'emploi** (APEC, 1jeune1solution) : ces derniers
  sont essayés au premier passage et gardés seulement s'ils renvoient des offres de stage.
- **Welcome to the Jungle** a refondu sa page de recherche (elle propose de créer un profil au lieu d'afficher les
  résultats) : la recherche directe ne renvoie plus rien. Crée une alerte e-mail « stage cybersécurité » sur WTTJ ; ses
  offres arrivent alors par les alertes e-mail, lues automatiquement.
- **IA en pause** : les recherches continuent même sans IA disponible ; les offres attendent et passent dès son retour.
- L'onglet **Sources** montre, pour aujourd'hui, les offres listées et les offres nouvelles de chaque source.

## Sites carrières des entreprises (repérés tout seuls)

Quand une candidature mène au site de l'entreprise (redirection HelloWork, Indeed, LinkedIn « Postuler »…), ce site est
retenu et surveillé directement : une offre y paraît souvent avant d'être reprise ailleurs. Tu peux aussi en ajouter un
dans l'onglet **Sources** (adresse du site carrière ou de la page des offres) et le tester tout de suite.

| Plateforme | Lecture |
|---|---|
| Greenhouse, Lever, Ashby, Workable, Recruitee, Workday, Personio, Breezy | Liste publique lue en une requête, sans navigateur, à chaque veille, avec la date de publication. |
| SmartRecruiters | Entreprise ajoutée à la liste du site SmartRecruiters. |
| Autres (page carrière maison, Taleo, Teamtailor, Talentsoft…) | Page lue dans Chrome aux passages complets. Les nouvelles offres sont repérées par différence avec la visite précédente. Si la page n'a pas d'offre, l'application suit le lien « Nos offres » et retient la bonne page. |

- Seuls les **stages en France publiés depuis la dernière lecture** sont retenus (24 h à la première lecture). Un lieu
  inconnu (petite ville, « 2 sites ») est gardé ; seul un lieu clairement à l'étranger est écarté, et l'IA vérifie
  ensuite que le poste est en France.
- **Workday** : les filtres du site de l'entreprise (France, stagiaires) sont utilisés ; les offres arrivent alors des
  plus récentes aux plus anciennes et sont lues jusqu'à sortir de la période (chez Thales : plus de 900 stages en France,
  ceux du jour trouvés en quelques secondes).
- Une source en erreur 5 fois de suite est mise de côté. Une source qui demande un **compte** pour postuler l'est aussi
  au bout de 2 offres. Toutes sont réactivables dans l'onglet Sources.
- Les formulaires de ces plateformes se ressemblent d'une entreprise à l'autre : la méthode mémorisée sur l'une sert pour
  les suivantes.
- **Ajouter une plateforme (développeur)** : dans `stageauto/sources.py`, une fonction décorée par `@plugin("id", "Nom")`
  qui renvoie la liste des offres d'une source, et un motif dans `detect()`.

## Réponses des recruteurs (toutes tes boîtes Gmail)

Les boîtes Gmail connectées dans Chrome sont relues à chaque passage complet, et toutes les heures pendant la veille.
Réglage **Comptes Gmail** : `0` et `1` par défaut (les deux premiers comptes de Chrome), ou tes adresses, une par ligne.
Deux numéros qui mènent au même compte ne sont lus qu'une fois.

- **🎉 Offre / acceptation** : candidature retenue, proposition de stage, convention de stage, « nous avons le plaisir de
  vous annoncer… », offer letter.
- **📅 Échange / entretien** : créneau proposé, disponibilités demandées, invitation à un appel ou à un entretien. Le lien
  de prise de rendez-vous (Calendly, Google Agenda, Outlook, HubSpot, Teams, Meet, Zoom…) est repéré : bouton
  « Choisir un créneau ».
- **Refus** (un refus poli contenant « félicitations pour votre parcours » reste un refus) et **réponse** (autre message).

Chaque réponse de recruteur est ouverte pour lire le message entier : la phrase décisive et le lien sont rarement dans
l'aperçu. Un e-mail non lu est remis en « non lu » après lecture, pour ne pas le rater dans ta boîte. Une offre ou un
échange proposé déclenche **tout de suite une notification** et s'affiche en tête de la carte **Aujourd'hui** pendant
7 jours ; tout est listé dans l'onglet **Réponses**, avec la boîte qui l'a reçu. Les lettres d'information des sites ne
sont pas prises pour des réponses. Le bouton **Exporter (CSV)** de l'onglet Offres donne toutes les candidatures, avec
la réponse reçue, dans un fichier pour Excel.

## Une seule fenêtre Chrome

- Tous les onglets de StageAuto (lecture des offres, LinkedIn, autres sites) s'ouvrent dans **une seule fenêtre** de ton
  Chrome ; LinkedIn et les autres sites y travaillent en parallèle, dans des onglets différents (réglage `single_window`).
- **Jamais de second Chrome** : si l'extension n'est pas connectée, l'application n'ouvre plus de « Chrome dédié ». Si
  Chrome est fermé, elle ouvre **ton** Chrome (ton profil, tes connexions) et attend l'extension ; sinon elle prévient
  « StageAuto attend Chrome » et le passage suivant réessaie, sans raccourcir la période de recherche (aucune offre perdue).
  Réglage `dedicated_chrome_fallback` pour revenir à l'ancien comportement.
- Le tableau de bord s'ouvre dans un **onglet** de ton Chrome, plus dans une fenêtre à part (réglage `dashboard_window`).
- Après la mise à jour, ferme une fois les anciennes fenêtres StageAuto en trop (celles des voies 1 et 2).

## Mon profil, mes fichiers, tous les paramètres

- **Onglet Mon profil** : tout ce que l'application sait de toi, modifiable. En haut, les réponses des formulaires
  (`personal/profile.json` : identité, contact, nationalité, disponibilité février 2027 et fin juillet 2027, formation
  avec les années, langues, permis B, certifications, mobilité…) : ce sont les seules réponses utilisées. En bas, le
  profil du skill (`references/profil.md` : expériences, projets, chiffres autorisés, outils interdits), source des CV.
  Pris en compte tout de suite ; l'ancienne version est gardée en `.bak`.
- **Onglet Mes fichiers** : où sont les CV et lettres, avec un bouton « Ouvrir » (Explorateur Windows) :
  - préparés : `%LOCALAPPDATA%\StageAuto\work\<Entreprise_Sujet>\out\` (un dossier par offre) ;
  - envoyés : `Téléchargements\Candidatures\envoyees\<Entreprise_Sujet>\` (copie de ce qui est parti) ;
  - et les derniers dossiers, avec un lien vers leurs PDF.
- **Réglages → Tous les paramètres** : chaque réglage de l'application en texte (JSON), y compris ceux sans case ;
  une erreur de format est signalée sans rien changer.

## Modèle local en dernier recours (Ollama)

Coché par défaut (Réglages → IA) : si toutes les autres IA échouent ou sont en pause (limite atteinte), un modèle local
Ollama prend le relais, toujours en dernier. Installe Ollama (ollama.com), puis dans un terminal `ollama pull qwen2.5:14b`
(ou `qwen2.5:7b` si le PC a peu de mémoire). Le modèle est choisi tout seul parmi ceux installés ; aucun réglage à faire.
Un petit modèle écrit moins bien les CV et remplit moins bien les formulaires : c'est un secours, pas un remplaçant.

## Règles (onglet Règles)

Modifiables à tout moment, appliquées à chaque passage :
- **Offres du jour seulement** (activé) : recherche limitée à depuis minuit ; une offre d'hier ou plus ancienne (date du
  site, « il y a 3 jours », « publiée hier ») est écartée avec la raison « règle : offre publiée avant aujourd'hui ».
- **LinkedIn d'abord** (activé) : les offres LinkedIn passent en tête pour la préparation des CV et les candidatures.
- **Langue du CV** (activé) : CV et lettre en anglais si l'offre demande un CV en anglais (« CV en anglais », « resume in
  English »…) ou est rédigée en anglais ; sinon en français. « Anglais courant exigé » ne suffit pas.
- **Mes règles** : une par ligne, transmises à l'IA pour le CV, la lettre et les formulaires. Les interdits du profil
  (nationalité, habilitation, compte à créer, données sensibles) restent toujours prioritaires.

## Réessayer les candidatures échouées

Une candidature qui n'a pas abouti (erreur, connexion demandée, formulaire bloquant) apparaît dans la carte
**Aujourd'hui** avec le bouton **Réessayer les échecs**, et dans l'onglet Offres (filtre « Échecs à réessayer ») avec un
bouton **Réessayer** par offre. Utile par exemple après t'être connecté à un site. Le bouton peut être recliqué autant de
fois que voulu.
- CV déjà prêt : la candidature repart directement ; sinon le CV est refait d'abord.
- Les offres écartées volontairement (non éligibles, pré-tri, déjà postulées) ne sont pas réessayées.
- Si un passage est en cours, le réessai démarre dès qu'il se termine. Il ne cherche pas de nouvelles offres et ne
  raccourcit pas la période de recherche du passage suivant.

## Vérifications

- **Avant l'envoi** : l'application relit le formulaire et bloque le clic sur « Envoyer » s'il reste un champ obligatoire
  vide, une case obligatoire non cochée, une question sans réponse, un fichier manquant ou un message d'erreur. L'agent
  corrige d'abord ; la relecture rapide passe la main à l'agent.
- **Après l'envoi** : la confirmation affichée est exigée. Sur LinkedIn, l'offre est rouverte pour vérifier
  « Candidature envoyée ». Les accusés de réception reçus dans Gmail sont rapprochés des candidatures. Le tableau de bord
  marque chaque candidature : ✓ confirmation affichée, confirmée par LinkedIn, accusé de réception reçu, ou ⚠ à vérifier.
- **Avant de postuler** : pas de doublon (même offre sur plusieurs sites), au plus 3 offres par entreprise et par jour, et
  aucune réponse sensible.

## Regarder l'application travailler

- **Curseur vert** dans les fenêtres StageAuto de ton Chrome. Il reste affiché d'une page à l'autre et porte une étiquette
  de ce qu'il fait : « Ouverture : linkedin.com », « Offre : … », « Lecture : … », « Saisie : Prénom », « Fichier : CV… »,
  « Clic : Suivant ».
- **Mode spectateur** (Réglages, activé par défaut) : le curseur glisse jusqu'à chaque champ avant de le remplir et parcourt
  les listes d'offres et les annonces en faisant défiler la page. C'est un peu plus lent (quelques secondes par
  formulaire) ; décoche-le pour la vitesse maximale.
- **Fenêtres de travail** : une par voie (lecture des offres, candidatures LinkedIn, autres sites). Chacune garde un petit
  onglet d'accueil épinglé et reste ouverte entre les passages : place-la une fois pour toutes, par exemple sur un second
  écran. Le bouton **« Voir Chrome »** du tableau de bord les met au premier plan.
- Ta vraie souris reste libre : le curseur vert est dessiné dans la page, il ne prend pas le contrôle de ta souris.

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

## Intelligence artificielle : au choix

Dans **Réglages → Intelligence artificielle**, choisis le fournisseur principal :

| Fournisseur | Ce qu'il faut |
|---|---|
| **Claude Code** (ton abonnement Claude) | Claude Code installé sur ce PC (version terminal, ou dans WSL), connexion faite une fois (taper `claude` dans un terminal, puis `/login`). Aucune clé. Plus lent qu'une clé API ; compte dans les limites de ton abonnement. |
| Claude (API Anthropic) | Clé créée sur console.anthropic.com (facturée à l'usage, distincte de l'abonnement Claude). |
| ChatGPT (OpenAI), Gemini (Google), Kimi (Moonshot), Qwen (Alibaba Cloud), DeepSeek, Mistral, OpenRouter, Groq | Clé du service + nom exact du modèle. |
| Modèle local : Ollama ou LM Studio | Le logiciel lancé sur ce PC + nom du modèle. Pas de clé. Un petit modèle remplit moins bien les formulaires. |
| Autre service compatible OpenAI | Adresse de l'API (…/v1), modèle, clé si besoin. |

- « Enregistrer et tester l'IA » vérifie le fournisseur en une question. La liste « État des fournisseurs » dit ce qui
  manque pour chacun.
- Avec « Si le fournisseur principal échoue… » coché, les autres fournisseurs prêts prennent le relais (clé refusée,
  crédit épuisé, service indisponible). Claude Code est utilisé automatiquement s'il est installé et qu'aucune clé n'est
  configurée.
- **Limite d'utilisation atteinte** (Claude Code : limite de 5 h, quotidienne ou hebdomadaire ; quota d'une API) :
  ce fournisseur est mis en pause jusqu'à l'heure de réinitialisation annoncée et les autres IA prêtes prennent le
  relais. Ajoute une deuxième IA (Gemini a une offre gratuite, ChatGPT, Kimi…) pour ne jamais t'arrêter. Sans autre IA,
  l'application continue de chercher, garde les offres et les CV déjà prêts, prévient une fois, puis relance un passage
  complet toute seule à l'heure de réinitialisation. L'en-tête du tableau de bord affiche « IA en pause : reprise HH:MM ».
- **Claude Code introuvable** : choisis Claude Code puis clique « Rechercher Claude Code sur ce PC ». La liste montre les
  emplacements essayés (installateur officiel `%USERPROFILE%\.local\bin\claude.exe`, npm, winget, WSL…). S'il n'est
  pas trouvé : dans un terminal, `where claude` (PowerShell : `(Get-Command claude).Source`), puis colle le chemin dans
  « Chemin de la commande claude ». Si la commande n'existe pas (Claude Code utilisé seulement dans l'application de
  bureau), installe la version terminal : PowerShell → `irm https://claude.ai/install.ps1 | iex`, puis tape `claude`
  une fois pour te connecter. Claude Code sous Windows demande aussi Git pour Windows (git-scm.com).
- Sans aucune IA prête, l'application cherche quand même et garde les offres « en attente de l'IA » ; elles sont reprises,
  dans l'ordre de priorité, dès qu'une IA est prête.

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
- Au plus 3 offres par entreprise et par jour, 20 invitations LinkedIn et 5 InMails par jour (réglables). Le nom de
  l'entreprise est comparé sans tenir compte de l'écriture (« Orange », « ORANGE SA », « Groupe Orange »).
- Jamais deux candidatures à la même offre, même publiée sur plusieurs sites : avant de préparer le CV puis juste avant
  d'envoyer, l'offre est comparée à celles déjà postulées (même entreprise, intitulé presque identique, ou intitulé abrégé
  de l'historique). Une redirection HelloWork ou Indeed vers une offre déjà postulée (orange.jobs, Thales…) est reconnue
  à son adresse et arrêtée tout de suite. Ces offres apparaissent « déjà postulée avant » et ne comptent pas dans les
  candidatures du jour.
- Formulaires « Formation » (établissement, diplôme, années « De » / « À ») : remplis avec la liste `etudes` du profil,
  exactement comme sur le CV.
- CV et lettre : uniquement les faits et chiffres du profil du skill, sans outil interdit.

## À savoir

- **Vérifié sur les vrais sites** (4 novembre 2026) : listes publiques Greenhouse, Lever, Ashby, Recruitee, Breezy,
  Workday et SmartRecruiters ; recherches orange.jobs, HelloWork et 1jeune1solution. **Pas vérifiable depuis
  l'environnement de développement** : Workable (refuse les serveurs), Personio (aucun exemple trouvé), APEC et Indeed
  (protection anti-robots), LinkedIn et JobTeaser (connexion nécessaire). Ces sites fonctionnent dans ton Chrome ;
  l'onglet **Sources** montre pour chaque source le dernier résultat ou la dernière erreur, et le bouton « Tester » la
  relit sur demande.
- **SmartRecruiters** : une entreprise sans aucune offre est signalée une fois par jour dans le journal (identifiant à
  vérifier : il figure dans l'adresse jobs.smartrecruiters.com/IDENTIFIANT). Exemple vérifié : « Capgemini » ne renvoie
  plus rien.
- **LinkedIn** interdit l'automatisation dans ses conditions d'utilisation. Les limites basses réduisent le risque de
  restriction du compte, sans le supprimer.
- **Coût** : compter grossièrement quelques dizaines de centimes de dollar par candidature (CV + agent). La facture
  Anthropic fait foi.
- **Après une mise à jour** : décompresse par-dessus le dossier puis double-clique `StageAuto.bat`. Si une ancienne version
  tourne encore en arrière-plan, elle est arrêtée et la nouvelle démarre. Le bouton « Redémarrer » des Réglages fait de même.
- **« Cannot access a chrome-extension:// URL of different extension »** : une autre extension (gestionnaire de mots de passe
  comme Proton Pass, correcteur…) insère son cadre dans la page, et Chrome bloque alors StageAuto. Depuis la version 1.4.0
  de l'extension, ces cadres sont retirés dans les fenêtres StageAuto seulement (tes autres fenêtres ne changent pas).
  Recharge l'extension dans chrome://extensions après la mise à jour : Chrome demande l'accès aux sites, à accepter.
- **Barre « StageAuto Bridge a démarré le débogage de ce navigateur »** : c'est normal, elle signale que l'application pilote
  ses fenêtres. Ne clique pas « Annuler » : cela coupe le pilotage en cours.
- **Un site connecté apparaît « non connecté »** : clique « Vérifier les connexions ». Sous chaque site s'affiche ce qui a
  été vu (ex. « session LinkedIn active (cookie li_at) », « redirigé vers la page de connexion »). Point orange = pas sûr :
  le site est utilisé quand même. Seul un rouge sûr fait sauter un site. Si tout est rouge, l'extension n'est sans doute
  pas connectée, ou elle est installée dans un autre profil Chrome que celui où tu es connecté.
- **« build du skill en échec »** (versions avant le 4 novembre) : sous Windows, `build.py` du skill s'arrêtait sur
  l'affichage de « ✓ » et chaque CV partait sur les modèles de secours. C'est corrigé : le skill tourne en UTF-8. Si
  `build.py` s'arrête encore (outil absent…), le journal affiche la cause en une ligne et un contrôle allégé prend le
  relais : le CV reste celui du skill.
- **Le tableau de bord ne s'ouvre pas** : lance **`StageAuto_debug.bat`**. Il démarre l'application avec une console qui
  affiche les messages ; envoie ces lignes si tu as besoin d'aide. Le détail est aussi dans
  `%LOCALAPPDATA%\StageAuto\console.log` et `stageauto.log`.
- **« Erreurs » sur l'extension** (chrome://extensions) : quand StageAuto est fermé, l'extension essaie de se reconnecter
  (après 30 s, 1 min, puis toutes les 2 min) et Chrome note chaque essai refusé (« WebSocket connection … failed »). C'est
  sans gravité : clique « Tout effacer ». Dès que l'application tourne, la connexion se fait toute seule, immédiatement
  si le tableau de bord s'ouvre, sinon en 2 min au plus. Le bouton « Reconnecter » de l'extension force la connexion.
- **Extension et pièces jointes** : si Chrome refuse de joindre un fichier, active « Autoriser l'accès aux URL de
  fichier » dans les détails de l'extension. L'application bascule aussi d'elle-même sur une méthode de secours.

## Structure

```
extension/            extension Chrome StageAuto Bridge (MV3, chrome.debugger)
stageauto/
  cdp.py              couche navigateur (extension ou Playwright), souris/clavier humains, curseur visible, instantané de page
  providers.py        fournisseurs d'IA : Claude ou service compatible OpenAI (clé personnelle), relais automatique
  replay.py           relecture des méthodes mémorisées sans IA
  priority.py         priorité des offres (récence, « premiers candidats ») et pré-tri sans IA
  agent.py            agent Claude (outils read_page, find, click, type, upload, auto_fill, fill_credentials, finish…)
  forms.py            réponses autorisées, remplissage automatique, détection des questions interdites
  cvskill.py          CV + lettre via le skill (SKILL.md + profil.md), build.py, correction, repli modèles
  sites.py            connexion, recherche et lecture des offres par site
  sources.py          sites carrières des entreprises et autres sites d'emploi (plugins par plateforme, repérage auto)
  skillrun.py         lance build.py du skill en UTF-8 (Windows)
  linkedin_contacts.py, gmail.py, pipeline.py, server.py, static/index.html
personal/             NON PUBLIÉ (.gitignore) : profile.json, skill/candidature-stage-ciblee, tpl/, seed/
personal.example/     structure attendue du dossier personnel
tests/                test hors ligne (formulaire local, agent simulé)
install.bat, StageAuto.bat (lancement discret), StageAuto_debug.bat (avec console), requirements.txt
```

Ligne de commande : `StageAuto.bat` (tableau de bord), `StageAuto.bat --run` (passage immédiat),
`.venv\Scripts\python -m stageauto.selftest` (vérifie LaTeX et les outils PDF).
