# myges-api

CLI, client Python et [documentation Swagger](https://arnaudmyges.github.io/myges-api/docs/) de
l'API myGES / Kordis, celle de l'application mobile myGES (ESGI, ECITV, PPA…).

> Projet **non officiel**, sans lien avec Kordis ni le Réseau GES. L'API n'est pas documentée
> publiquement et peut changer sans préavis.

## Installation

```bash
sudo apt install pipx && pipx ensurepath   # une seule fois, puis ouvrir un nouveau terminal
pipx install "myges-api[cli] @ git+https://github.com/Arnaudmyges/myges-api"
```

## CLI

```bash
myges login                      # une seule fois : identifiant + mot de passe myGES

myges agenda                     # 7 prochains jours
myges agenda --from 2026-11-02 --to 2026-11-06 --ics semaine.ics
myges grades                     # notes de l'année ; -y 2025 pour une autre
myges absences
myges courses                    # matières (avec leur rc_id)
myges files                      # supports de cours
myges files --download ~/cours   # tout télécharger ; relancer ne récupère que les nouveaux
myges syllabus <rc_id>
myges projects
myges practicals                 # TP
myges steps                      # prochaines échéances
myges token                      # token à coller dans « Authorize » du Swagger
```

`myges --help` liste toutes les commandes, et `--json` donne la sortie brute de chacune.
La session dure ~6 jours ; ensuite, relancer `myges login`.

## Bibliothèque

```python
from myges import MyGES

with MyGES("jdupont", "motdepasse") as ges:
    for note in ges.grades():
        print(note.course, note.average)

    for matiere in ges.courses():
        for support in ges.course_files(matiere.rc_id):
            data = ges.download_course_file(matiere.rc_id, support.oc_id)
```

Une version asynchrone (`AsyncMyGES`) expose les mêmes méthodes.

## Voir aussi

- [Swagger](https://arnaudmyges.github.io/myges-api/docs/) · [Redoc](https://arnaudmyges.github.io/myges-api/docs/redoc.html) · [spec OpenAPI](openapi/myges.yaml)
- [Notes sur l'API](docs/api-notes.md) : particularités observées, historisées
- [CONTRIBUTING.md](CONTRIBUTING.md) : développement, ajout d'un endpoint

Licence MIT.
