# myGES API : CLI, bibliothèque Python et doc Swagger non officielles

<p align="center">
  <a href="https://arnaudmyges.github.io/myges-api/docs/"><img alt="Swagger UI" src="https://img.shields.io/badge/Swagger_UI-docs-85EA2D?logo=swagger&logoColor=black&style=for-the-badge"></a>
  <a href="https://arnaudmyges.github.io/myges-api/docs/redoc.html"><img alt="Redoc" src="https://img.shields.io/badge/Redoc-r%C3%A9f%C3%A9rence-E53935?logo=readthedocs&logoColor=white&style=for-the-badge"></a>
  <a href="https://github.com/Arnaudmyges/myges-api/blob/main/openapi/myges.yaml"><img alt="OpenAPI 3.1" src="https://img.shields.io/badge/OpenAPI-3.1-6BA539?logo=openapiinitiative&logoColor=white&style=for-the-badge"></a>
  <a href="https://pypi.org/project/myges-api/"><img alt="PyPI" src="https://img.shields.io/pypi/v/myges-api?style=for-the-badge&logo=pypi&logoColor=white"></a>
  <a href="https://arnaudmyges.github.io/myges-api/docs/"><img alt="33 endpoints" src="https://img.shields.io/badge/endpoints-33-blue?style=for-the-badge"></a>
</p>

**myGES dans ton terminal** : emploi du temps, notes, supports de cours en un clic, alertes
sur ton téléphone. Pour les étudiants de l'**ESGI** et des autres écoles du **Réseau GES**
qui utilisent myGES. Avec la documentation Swagger complète de l'API myGES.

<p align="center">
  <a href="https://arnaudmyges.github.io/myges-api/docs/">
    <img alt="Documentation Swagger de l'API myGES" src="https://raw.githubusercontent.com/Arnaudmyges/myges-api/main/docs/assets/swagger.png" width="50%">
  </a>
</p>

> Projet **non officiel**, sans lien avec Kordis ni le Réseau GES.

## Installation

```bash
sudo apt install pipx && pipx ensurepath     # puis ouvrir un nouveau terminal
pipx install "myges-api[cli]"
myges login
```

macOS, Windows, mise à jour : [installation](https://github.com/Arnaudmyges/myges-api/blob/main/docs/installation.md).

## Utilisation

```bash
myges next                   # ton prochain cours, salle, prof
myges agenda                 # la semaine
myges grades                 # tes notes
myges files --download       # tous les supports de cours, rangés par matière
myges teachers --mail python # écrire à ton prof de Python (brouillon Outlook)
myges --help                 # tout le reste
```

Toutes les commandes : [docs/cli.md](https://github.com/Arnaudmyges/myges-api/blob/main/docs/cli.md).

## Alertes

Une nouvelle note, un nouveau support, un cours annulé ? Reçois-le sur ton téléphone, Discord
ou par e-mail : [alertes](https://github.com/Arnaudmyges/myges-api/blob/main/docs/alertes.md).

## Assistant (Claude)

« Quelle est ma moyenne ? », « En quelle salle est mon cours de Python ? », « Écris au prof
que j'ai oublié Edusign » : pose tes questions à Claude grâce au serveur MCP,
[assistant](https://github.com/Arnaudmyges/myges-api/blob/main/docs/assistant.md).

## Google Calendar

Ton emploi du temps dans Google Calendar, toujours à jour : [synchro Google Calendar](https://github.com/Arnaudmyges/myges-api/blob/main/docs/google-calendar.md).

## Questions fréquentes

<details>
<summary><b>Comment exporter son emploi du temps myGES vers Google Calendar ?</b></summary>

`myges gcal sync` tient un agenda Google Calendar à jour tout seul (cours ajoutés, déplacés,
annulés). Pour un export ponctuel vers n'importe quel agenda : `myges agenda --ics cours.ics`.
</details>

<details>
<summary><b>Comment télécharger tous les supports de cours myGES d'un coup ?</b></summary>

`myges files --download` : un dossier par matière. Relancer la commande ne récupère que les
nouveaux supports.
</details>

<details>
<summary><b>Comment être prévenu d'une nouvelle note ou d'un nouveau support ?</b></summary>

`myges notify add ntfy` (ou Discord, e-mail) puis `myges watch --install` : les nouveaux
supports arrivent en moins de 5 minutes.
</details>

<details>
<summary><b>Comment s'inscrire à un événement myGES dès l'ouverture des inscriptions ?</b></summary>

`myges events subscribe 4525` (l'id est à la fin de l'adresse de l'événement) : l'inscription
part à la seconde où elles ouvrent.
</details>

<details>
<summary><b>Mon mot de passe est-il en sécurité ?</b></summary>

Il n'est envoyé qu'au serveur d'authentification de myGES (Kordis). Ta machine garde la
session (un jeton valable ~6 jours), et le mot de passe seulement si tu le demandes
(`--save-password`, pour les alertes automatiques), dans des fichiers lisibles par toi seul.
Le code est ouvert : tout se vérifie.
</details>

## Pour les développeurs

[Swagger](https://arnaudmyges.github.io/myges-api/docs/) ·
[bibliothèque Python](https://github.com/Arnaudmyges/myges-api/blob/main/docs/bibliotheque.md) ·
[notes sur l'API](https://github.com/Arnaudmyges/myges-api/blob/main/docs/api-notes.md) ·
[contribuer](https://github.com/Arnaudmyges/myges-api/blob/main/CONTRIBUTING.md) — licence MIT.
