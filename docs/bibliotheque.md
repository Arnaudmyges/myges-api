# Bibliothèque Python

```bash
pip install myges-api
```

Dépendances : `httpx` et `pydantic` uniquement (la CLI est dans l'extra `[cli]`).

```python
from myges import MyGES

with MyGES("jdupont", "motdepasse") as ges:  # ou MyGES.from_env()
    for note in ges.grades():  # année la plus récente par défaut
        print(note.course, note.average)

    for matiere in ges.courses():
        for support in ges.course_files(matiere.rc_id):
            data: bytes = ges.download_course_file(matiere.rc_id, support.oc_id)
```

Version asynchrone, mêmes méthodes :

```python
from myges import AsyncMyGES

async with AsyncMyGES.from_env() as ges:
    projets = await ges.projects()
    async for actu in ges.iter_news():
        ...
```

## Comportements

- **Token** : mis en cache dans `~/.cache/myges/tokens.json` (`0600`, un par identifiant). Sur
  un `401`, le client se ré-authentifie une fois si le mot de passe est disponible. Cache
  personnalisable : `token_store=MemoryTokenStore()` ou votre propre `TokenStore`.
- **Années** : `year=None` désigne la dernière année de `/me/years` ; `2026` = 2026-2027.
- **Listes vides** : l'API renvoie parfois `204` sans corps ; le client renvoie `[]`.
- **Dates** : converties en `datetime` UTC.
- **Champs inconnus** : conservés dans `model.model_extra` ; un ajout côté Kordis ne casse rien.
- **Écritures** (rejoindre / quitter un groupe, message, inscription à un événement) : jamais
  vérifiées, elles lèvent `WriteNotAllowedError` sauf avec `MyGES(..., allow_writes=True)`.
- **Erreurs** : `InvalidCredentialsError`, `TokenExpiredError`, `NotFoundError`,
  `BadRequestError`, `BlockedRequestError`, toutes dérivées de `MyGESError`. Les erreurs de
  validation ne recopient jamais les données reçues.

## Modules annexes

- `myges.ics.to_ics(events)` : export iCalendar.
- `myges.watch` : instantanés et détection de changements (sans réseau).
- `myges.blocs` : moyennes pondérées par bloc, simulation.
- `myges.gcal` : synchro Google Calendar (extra `[gcal]`).
- `myges.notify` : envoi ntfy / Discord / e-mail.

Référence complète des endpoints : [Swagger](https://arnaudmyges.github.io/myges-api/docs/).
