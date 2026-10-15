# myges-api

<p align="center">
  <a href="https://arnaudmyges.github.io/myges-api/docs/"><img alt="Swagger UI" src="https://img.shields.io/badge/Swagger_UI-docs-85EA2D?logo=swagger&logoColor=black&style=for-the-badge"></a>
  <a href="https://arnaudmyges.github.io/myges-api/docs/redoc.html"><img alt="Redoc" src="https://img.shields.io/badge/Redoc-r%C3%A9f%C3%A9rence-E53935?logo=readthedocs&logoColor=white&style=for-the-badge"></a>
  <a href="openapi/myges.yaml"><img alt="OpenAPI 3.1" src="https://img.shields.io/badge/OpenAPI-3.1-6BA539?logo=openapiinitiative&logoColor=white&style=for-the-badge"></a>
  <a href="https://arnaudmyges.github.io/myges-api/docs/"><img alt="33 endpoints" src="https://img.shields.io/badge/endpoints-33-blue?style=for-the-badge"></a>
</p>

CLI, client Python et documentation Swagger de l'API myGES / Kordis, celle de l'application
mobile myGES (ESGI, ECITV, PPA…).

<p align="center">
  <a href="https://arnaudmyges.github.io/myges-api/docs/">
    <img alt="Documentation Swagger de l'API myGES" src="docs/assets/swagger.png" width="50%">
  </a>
</p>

> Projet **non officiel**, sans lien avec Kordis ni le Réseau GES. L'API n'est pas documentée
> publiquement et peut changer sans préavis.

## Installation

Nécessite Python ≥ 3.10. On installe d'abord [pipx](https://pipx.pypa.io) (une seule fois),
qui rend la commande `myges` disponible partout.

**Linux (Debian / Ubuntu) et WSL**

```bash
sudo apt install pipx
pipx ensurepath
```

**macOS**

```bash
brew install pipx
pipx ensurepath
```

**Windows** : le plus simple est WSL (section Linux ci-dessus). Sinon, en natif (PowerShell, avec Python depuis [python.org](https://www.python.org/downloads/) ou le Microsoft Store)

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

Puis, **dans un nouveau terminal**, sur tous les systèmes :

```bash
pipx install "myges-api[cli] @ https://github.com/Arnaudmyges/myges-api/archive/refs/heads/main.zip"
```

Mise à jour : relancer la même commande avec `--force`.

## CLI

```bash
myges login                      # une seule fois : identifiant + mot de passe myGES

myges agenda                     # 7 prochains jours
myges agenda --from 2026-11-02 --to 2026-11-06 --ics semaine.ics
myges grades                     # notes de l'année ; -y 2025 pour une autre
myges absences
myges courses                    # matières (avec leur rc_id)
myges files                      # supports de cours
myges files --download          # tout télécharger dans le dossier courant (ou --download ~/cours)
                                 # relancer ne récupère que les nouveaux
myges syllabus <rc_id>
myges projects
myges practicals                 # TP
myges steps                      # prochaines échéances
myges token                      # token à coller dans « Authorize » du Swagger
```

Sous WSL, pour retrouver les supports dans l'Explorateur Windows, télécharger vers le disque
Windows : `myges files --download /mnt/c/Users/<toi>/Documents/cours`.

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
