# myges-api

<p align="center">
  <a href="https://arnaudmyges.github.io/myges-api/docs/"><img alt="Swagger UI" src="https://img.shields.io/badge/Swagger_UI-docs-85EA2D?logo=swagger&logoColor=black&style=for-the-badge"></a>
  <a href="https://arnaudmyges.github.io/myges-api/docs/redoc.html"><img alt="Redoc" src="https://img.shields.io/badge/Redoc-r%C3%A9f%C3%A9rence-E53935?logo=readthedocs&logoColor=white&style=for-the-badge"></a>
  <a href="openapi/myges.yaml"><img alt="OpenAPI 3.1" src="https://img.shields.io/badge/OpenAPI-3.1-6BA539?logo=openapiinitiative&logoColor=white&style=for-the-badge"></a>
  <a href="https://arnaudmyges.github.io/myges-api/docs/"><img alt="33 endpoints" src="https://img.shields.io/badge/endpoints-33-blue?style=for-the-badge"></a>
</p>

**myGES dans ton terminal** : emploi du temps, notes, supports de cours en un clic, alertes
sur ton téléphone. Et la documentation Swagger complète de l'API myGES.

<p align="center">
  <a href="https://arnaudmyges.github.io/myges-api/docs/">
    <img alt="Documentation Swagger de l'API myGES" src="docs/assets/swagger.png" width="50%">
  </a>
</p>

> Projet **non officiel**, sans lien avec Kordis ni le Réseau GES.

## Installation

```bash
sudo apt install pipx && pipx ensurepath      # macOS : brew install pipx — Windows : voir docs
pipx install "myges-api[cli] @ https://github.com/Arnaudmyges/myges-api/archive/refs/heads/main.zip"
myges login
```

Autres systèmes, mise à jour, complétion : [docs/installation.md](docs/installation.md).

## Utilisation

```bash
myges next                   # ton prochain cours, salle, prof
myges agenda                 # la semaine
myges grades                 # tes notes
myges files --download       # tous les supports de cours, rangés par matière
myges --help                 # tout le reste
```

Toutes les commandes : [docs/cli.md](docs/cli.md).

## Alertes

Une nouvelle note, un nouveau support, un cours annulé ? Reçois-le sur ton téléphone, Discord
ou par e-mail : [docs/alertes.md](docs/alertes.md).

## Google Agenda

Ton emploi du temps dans Google Agenda, toujours à jour : [docs/google-calendar.md](docs/google-calendar.md).

## Pour les développeurs

[Swagger](https://arnaudmyges.github.io/myges-api/docs/) ·
[bibliothèque Python](docs/bibliotheque.md) ·
[notes sur l'API](docs/api-notes.md) ·
[contribuer](CONTRIBUTING.md) — licence MIT.
