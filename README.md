# myges-api

Spécification **OpenAPI 3.1**, client **Python typé** (sync + async) et **CLI** pour l'API
myGES / Kordis, celle qu'utilise l'application mobile myGES du Réseau GES (ESGI, ECITV, PPA…).

> Projet **non officiel**, sans lien avec Kordis ni le Réseau GES. Ni l'un ni l'autre ne
> documentent cette API. Tout ce qui est décrit ici a été observé sur l'API réelle, et peut
> changer sans préavis.

- 📖 **Documentation interactive** : [Swagger UI](https://arnaudmyges.github.io/myges-api/docs/) · [Redoc](https://arnaudmyges.github.io/myges-api/docs/redoc.html)
- 📄 **Spécification** : [`openapi/myges.yaml`](openapi/myges.yaml)

## Ce que contient le dépôt

| | |
|---|---|
| `openapi/myges.yaml` | Spec OpenAPI 3.1 : 33 opérations, 45 schémas, erreurs, pagination, auth. Chaque opération indique sa date de vérification (`x-verified`). |
| `src/myges/` | Client `MyGES` / `AsyncMyGES` : modèles Pydantic, dates converties en `datetime`, cache du token, ré-authentification automatique. |
| `myges` (CLI) | Agenda, notes, supports de cours, syllabus, projets, TP… en tableau, en JSON ou en `.ics`. |
| `tests/` | Tests unitaires sur des fixtures **synthétiques** et un **test de contrat** qui valide la vraie API contre la spec. |

## Installation

```bash
pip install "myges-api[cli] @ git+https://github.com/Arnaudmyges/myges-api"
```

Python ≥ 3.10. Sans l'extra `[cli]`, seules `httpx` et `pydantic` sont installées.

## CLI

```bash
export MYGES_USERNAME=jdupont
myges login                 # demande le mot de passe et met le token en cache (~6 jours)

myges agenda                # 7 prochains jours
myges agenda --from 2026-11-02 --to 2026-11-06 --ics semaine.ics
myges grades                # année en cours ; -y 2025 pour une autre
myges absences --json
myges files                 # supports de cours de l'année
myges files --download ~/cours   # tout télécharger (un dossier par matière, reprise incrémentale)
myges syllabus 500001       # syllabus d'une matière (rc_id de `myges courses`)
myges projects
myges practicals            # TP
myges steps                 # prochaines échéances de projet
myges raw /me/2026/courses  # GET brut sur n'importe quel chemin
myges token                 # token à coller dans « Authorize » de la doc Swagger
```

`myges --help` liste toutes les commandes. Pour un usage non interactif (cron),
`MYGES_PASSWORD` dans l'environnement permet de renouveler le token automatiquement.

## Bibliothèque

```python
from datetime import date, timedelta
from pathlib import Path

from myges import MyGES

with MyGES("jdupont", "motdepasse") as ges:  # ou MyGES.from_env()
    for cours in ges.agenda(date.today(), date.today() + timedelta(days=7)):
        print(cours.start_date, cours.name, cours.location, cours.teacher)

    for note in ges.grades():  # année la plus récente par défaut
        print(note.course, note.grades, note.average, note.coef_value)

    for matiere in ges.courses():
        for support in ges.course_files(matiere.rc_id):
            contenu: bytes = ges.download_course_file(matiere.rc_id, support.oc_id)
            Path(support.filename).write_bytes(contenu)
```

En asynchrone, mêmes méthodes (un test vérifie qu'elles restent identiques) :

```python
from myges import AsyncMyGES

async with AsyncMyGES.from_env() as ges:
    projets = await ges.projects()
    async for actu in ges.iter_news():
        ...
```

### Comportements à connaître

- **Token** : mis en cache dans `~/.cache/myges/tokens.json` (`0600`, un par identifiant).
  Sur un `401`, le client se ré-authentifie une fois, si le mot de passe est disponible.
  Cache personnalisable : `token_store=MemoryTokenStore()` ou votre propre `TokenStore`.
- **Années** : `year=None` désigne la dernière année de `/me/years`. `2026` correspond à 2026-2027.
- **Listes vides** : l'API renvoie parfois `204` sans corps ; le client renvoie `[]`.
- **Champs inconnus** : conservés dans `model.model_extra`, donc un ajout côté Kordis ne casse rien.
- **Écritures** (rejoindre ou quitter un groupe, envoyer un message, s'inscrire à un
  événement) : jamais vérifiées par
  ce projet, elles lèvent `WriteNotAllowedError` sauf si le client est créé avec `allow_writes=True`.
- **Erreurs** : `InvalidCredentialsError`, `TokenExpiredError`, `NotFoundError`,
  `BadRequestError`, `BlockedRequestError`, toutes dérivées de `MyGESError`. Les erreurs
  de validation ne recopient jamais les données reçues.

## Particularités de l'API

Le détail est dans la [description de la spec](openapi/myges.yaml). En résumé :

- **Auth** : OAuth *implicit grant* détourné. Il faut envoyer les identifiants en Basic Auth
  à `authentication.kordis.fr`, puis lire le token dans le fragment de la redirection `302`
  vers `comreseaugesskolae:/…`. Il n'y a pas de refresh token, et une nouvelle auth renvoie le
  **même** token tant qu'il est actif.
- **Pare-feu** : `403` si le User-Agent est `python-requests/*` (ou absent sur l'auth).
- **Enveloppe** `{response_code, version, result, links}` partout. Les dates sont en
  millisecondes epoch.
- **401 en XML**, les autres erreurs en JSON façon Spring Boot.
- **HAL incohérent** : `links` (tableau) dans les listes, `_links` (objet) dans les détails.
- **Champs piégeux** : `coef`/`ects` sont des nombres dans `/courses` mais des chaînes
  (`"N.C."`) dans `/grades` ; `rooms`, `steps`, `project_files`, `psp_limit_date` peuvent valoir `null`.
- **`/me/{x}/…` ambigu** : certaines routes prennent une année (`/me/2026/grades`), d'autres
  un `rc_id` de matière (`/me/500001/files`, `/me/500001/syllabus`). Une mauvaise valeur ne
  produit pas d'erreur claire (`/me/2026/syllabus` répond `200 []`).
- **Supports de cours** : `GET /me/{rc_id}/files` les liste, et `GET /me/{rc_id}/files/{oc_id}`
  renvoie le fichier brut (avec un `Content-Type: application/json` erroné). Les liens
  `ges-dl.kordis.fr/private/…` exigent eux une session web CAS.
- **Réservé à d'autres rôles** : `/me/{year}/students` et `/me/campus/{id}/rooms` (`418`),
  `/planning/…` (`401`, autre scope).
- **Uniquement sur le site web** : documents administratifs (certificats de scolarité…),
  factures, points OPEN.

## Développement

```bash
git clone https://github.com/Arnaudmyges/myges-api && cd myges-api
uv sync --group dev
make check                 # ruff + mypy --strict + pytest
make live                  # contrat contre la vraie API (identifiants requis)
make docs                  # Swagger UI + Redoc sur http://localhost:8000/docs/
```

Voir [CONTRIBUTING.md](CONTRIBUTING.md), en particulier pour **documenter un nouvel endpoint**
et **ne jamais committer de données réelles**.

## Licence

MIT.
