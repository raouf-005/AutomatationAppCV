"""Configuration : chemins, profil du candidat, limites et réglages modifiables depuis l'interface."""
import json
import os
import sys
from pathlib import Path

APP_NAME = "StageAuto"
PKG = Path(__file__).resolve().parent
IS_WIN = sys.platform.startswith("win")

# Données de l'application (base SQLite, profil Chrome dédié, journaux)
DATA = Path(os.environ.get("STAGEAUTO_DATA") or
            (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / APP_NAME if IS_WIN
             else Path.home() / ".stageauto"))
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "stage.db"
SETTINGS_PATH = DATA / "settings.json"
CHROME_PROFILE = DATA / "ChromeProfile"
LOG_PATH = DATA / "stageauto.log"
WORK = DATA / "work"

DOWNLOADS = Path.home() / "Downloads"
CANDIDATURES = DOWNLOADS / "Candidatures"

PORT = int(os.environ.get("STAGEAUTO_PORT", "8765"))

# Données personnelles : dossier « personal/ » à côté de l'application (jamais publié sur GitHub).
PERSONAL = Path(os.environ.get("STAGEAUTO_PERSONAL") or (PKG.parent / "personal"))
SKILL_DIR = PERSONAL / "skill" / "candidature-stage-ciblee"
TPL_DIR = PERSONAL / "tpl"
SEED_DIR = PERSONAL / "seed"
_profile_file = PERSONAL / "profile.json"
if not _profile_file.exists():
    _profile_file = PKG.parent / "personal.example" / "profile.json"
# Profil : seules réponses personnelles autorisées (jamais d'autres données sensibles).
PROFILE = json.loads(_profile_file.read_text(encoding="utf-8"))

# Offres à abandonner : données sensibles non autorisées ou conditions non remplies.
BLOCK_PATTERNS = [
    r"titre de s[ée]jour", r"autorisation de travail", r"work (permit|authori[sz]ation)", r"authori[sz]ed to work",
    r"casier", r"criminal", r"s[ée]curit[ée] sociale", r"social security", r"passeport|passport",
    r"date de naissance|birth ?date|date of birth|lieu de naissance", r"\biban\b|\bbic\b|coordonn[ée]es bancaires|bank account|rib\b",
    r"vid[ée]o", r"habilitation|secret d[ée]fense|security clearance", r"nationalit[ée] fran[çc]aise",
    r"citoyennet[ée]|citizenship|citizen of", r"carte d'identit[ée]|identity card|num[ée]ro d'identit",
]

BANNED_TOOLS = ["IIS", "Jenkins", "Sonar", "Power BI", "AWS", "Azure", "DORA", "IS-IS", "SRv6"]

DOMAIN_POS = [
    "cyber", "sécurité", "securite", "security", "soc", "cert", "csirt", "pentest", "offensi", "red team",
    "grc", "conformité", "compliance", "iso 27001", "devops", "devsecops", "sre", "cloud", "infra",
    "réseau", "reseau", "network", "système", "systeme", "sysadmin", "admin sys", "linux", "kubernetes",
    "automatisation", "automation", "iam", "identit", "siem", "vulnérab", "audit", "risque", "netdevops",
]
DOMAIN_NEG = [
    "alternance", "apprenti", "cdi", "cdd", "commercial", "business developer", "vente", "marketing",
    "rh ", "ressources humaines", "juridique", "juriste", "fpga", "électronique", "comptab", "finance ",
    "data scientist", "stage de 3 mois", "stage de 4 mois", "2 mois", "3 mois", "4 mois", "5 mois",
]

DEFAULT_SETTINGS = {
    "model": "claude-opus-5-5",
    "browser_backend": "extension",   # extension = ton Chrome habituel ; playwright = Chrome dédié
    "linkedin_real_chrome_only": True,  # LinkedIn uniquement via ton vrai Chrome (extension), jamais le Chrome dédié
    "human_mode": "linkedin",         # souris/clavier humains : linkedin | all | off
    "show_cursor": True,              # curseur visible dans la page pendant que l'application agit
    "spectator_mode": True,           # le curseur glisse, parcourt les pages et annonce ce qu'il fait (un peu plus lent)
    "use_memory": True,               # rejouer les méthodes mémorisées avant d'appeler l'IA
    "llm_provider": "anthropic",      # anthropic | claude_code | openai | gemini | kimi | qwen | deepseek | mistral |
                                      # openrouter | groq | ollama | lmstudio | custom  (voir providers.py)
    "llm_fallback": True,             # les autres fournisseurs configurés prennent le relais si le premier échoue
    "models": {},                     # modèle par fournisseur (nom exact chez le fournisseur)
    "custom_base_url": "",            # « Autre service compatible OpenAI »
    "claude_code_path": "",           # chemin de la commande claude si elle n'est pas trouvée toute seule
    "foreign_max_tokens": 16000,
    "chrome_path": "",
    "agent_effort": "low",
    "cv_effort": "medium",
    "agent_max_steps": 40,
    "gmail_index": 0,
    "schedule": ["11:00", "19:00"],    # passages complets
    "watch_enabled": True,            # veille : vérifier souvent les nouvelles offres pour postuler parmi les premiers
    "watch_interval_min": 30,
    "watch_hours": "08:00-22:00",
    "linkedin_watch_min": 60,         # LinkedIn : une recherche par heure au plus en veille
    "watch_sites": [],                # vide = tous les sites actifs
    "rule_only_today": True,          # règles (onglet Règles) : offres publiées aujourd'hui seulement
    "rule_linkedin_first": True,      # LinkedIn d'abord
    "rule_cv_lang": True,             # CV en anglais si l'offre le demande (ou est en anglais), sinon en français
    "rules_custom": "",               # règles libres, une par ligne, transmises à l'IA
    "sweep_time": "22:30",            # rattrapage du soir : toutes les sources sur 26 h (vide = désactivé)
    "sources_pages_per_pass": 12,     # sites carrières lus dans Chrome par passage complet (tous au rattrapage)
    "max_result_pages": 4,            # pages de résultats lues par recherche lors des passages complets
    "gmail_alerts": True,             # lire les alertes e-mail des sites d'emploi pour ne rater aucune offre
    "gmail_every_min": 60,            # Gmail (réponses, entretiens, offres) relu pendant la veille toutes les N minutes
    "gmail_accounts": ["0", "1"],     # boîtes Gmail lues : numéros u/0, u/1… ou adresses (les deux premiers comptes de Chrome)
    "parallel_lanes": True,           # LinkedIn et autres sites postulent en parallèle (2 fenêtres)
    "prescreen": True,                # écarter sans IA les offres manifestement hors cible
    "cv_mode": "skill",               # skill (qualité maximale) | rapide (modèles, ~4x plus rapide)
    "auto_submit": True,              # False = remplit tout puis attend la validation dans l'interface
    "max_per_company_per_day": 3,
    "max_applications_per_pass": 40,
    "max_invites_per_day": 20,
    "max_inmails_per_day": 5,
    "linkedin_searches": [
        "stage cybersécurité", "stage DevOps OR DevSecOps OR cloud OR réseau OR SOC OR pentest OR GRC"],
    "keywords": ["stage cybersécurité", "stage SOC", "stage pentest", "stage DevOps", "stage DevSecOps",
                 "stage réseau sécurité", "stage GRC", "stage cloud infrastructure", "stage administrateur systèmes"],
    "jobteaser_base": "https://www.jobteaser.com",
    "smartrecruiters_companies": ["Wavestone1", "SopraSteria1", "Capgemini", "Ubisoft2", "Equativ", "Sanofi"],
    "teamtailor_hosts": ["careers.almond.eu", "recrutement.volkswagengroup.fr"],
    "sites_enabled": {"linkedin": True, "jobteaser": True, "wttj": True, "indeed": True, "hellowork": True,
                      "smartrecruiters": True, "orange": True, "carrieres": True},
    "parallel_tabs": 3,
    "parallel_llm": 4,
    "ntfy_topic": "",                 # notifications téléphone via ntfy.sh (facultatif)
    "headless": False,
    "certificats_pdf": str(CANDIDATURES / "Certifications_Abderraouf_Derardja.pdf"),
    "custom_answers": {},             # question -> réponse ajoutées dans l'interface
    "defense_companies": ["Thales", "MBDA", "Naval Group", "Airbus", "Safran", "Dassault", "ArianeGroup",
                          "Arquus", "KNDS", "Nexter"],
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            s.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return s


def save_settings(s: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
