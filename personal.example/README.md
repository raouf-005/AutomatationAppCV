# Dossier personnel (non publié)

L'application lit les données personnelles dans `personal/` (à côté de `stageauto/`), qui n'est **jamais** envoyé sur GitHub :

```
personal/
  profile.json                         réponses autorisées des formulaires (identité, contact, disponibilité…)
  skill/candidature-stage-ciblee/      le skill CV + lettre (SKILL.md, références, gabarit, polices, photo, build.py)
  tpl/                                 modèles de CV/lettre de secours (gen.py)
  seed/                                historique importé au premier lancement (seen.txt, contacts.md, NOTES.md)
```

Décompresse `StageAuto_personnel.zip` à la racine de l'application pour obtenir ce dossier.
Ce dossier-ci (`personal.example/`) ne sert que d'exemple de structure.
