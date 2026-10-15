# Contribuer

## Mise en place

```bash
git clone https://github.com/Arnaudmyges/myges-api && cd myges-api
uv sync --group dev        # ou : python3 -m venv .venv && .venv/bin/pip install -e '.[cli]' (+ outils de dev)
make check                 # ruff + mypy --strict + pytest
make live                  # contrat contre la vraie API (MYGES_USERNAME / MYGES_PASSWORD)
make docs                  # Swagger UI + Redoc sur http://localhost:8000/docs/
```

Les particularités de l'API sont consignées dans [docs/api-notes.md](docs/api-notes.md) :
toute nouvelle découverte y ajoute une entrée datée.

## Règle n°1 : aucune donnée réelle dans le dépôt

Les réponses de l'API contiennent des données personnelles : adresse, INE, notes, noms et
emails des camarades. Elles ne doivent apparaître ni dans un commit, ni dans une issue, ni
dans une PR.

- Les fixtures de `tests/fixtures/` sont **inventées** : noms fictifs, identifiants
  arbitraires, domaines `example.org`. Pour en ajouter une, recopier la *structure* d'une
  vraie réponse et remplacer **toutes** les valeurs.
- Les captures brutes vont dans `captures/` (ignoré par git).
- Pour signaler un écart, donner le chemin JSON et le type (`$.result[].steps: null au lieu
  d'un tableau`), jamais la valeur.

## Documenter un nouvel endpoint

1. Le trouver : onglet Réseau des DevTools sur myGES, liens `_links` de `/me/profile`, ou
   `myges raw /me/...`. Ne sonder **que** des `GET`.
2. Observer plusieurs réponses (plusieurs années, comptes et éléments). Un champ vu une seule
   fois non nul peut très bien être nullable.
3. L'ajouter à `openapi/myges.yaml` avec `operationId`, `tags`, `summary`, les réponses
   (y compris `204`, `401`, `404`) et `x-verified: "AAAA-MM-JJ"`.
4. Ajouter le modèle dans `src/myges/models.py`, puis la méthode dans **les deux** clients
   (`MyGES` et `AsyncMyGES`), et la commande CLI si elle est utile.
5. Ajouter `tests/fixtures/<operationId>.json` (synthétique), puis l'entrée correspondante
   dans `tests/test_models.py` et `tests/test_live.py`.
6. `make check`, puis `make live` avec vos identifiants.
7. Consigner ce qui a été appris dans `docs/api-notes.md`.

Les tests vérifient automatiquement que :

- la spec est un OpenAPI 3.1 valide ;
- chaque opération `GET` a une fixture conforme à son schéma ;
- chaque fixture se charge dans son modèle **sans champ non déclaré** ;
- les clients sync et async exposent les mêmes méthodes.

## Endpoints en écriture

Ne pas les tester sur un vrai compte sans accord explicite des personnes concernées (un
message de groupe est visible par tous les membres et par l'intervenant). Ils restent
`x-verified: false` et derrière `allow_writes=True` tant qu'ils n'ont pas été vérifiés.

## Style

`ruff` (lint + format), `mypy --strict`. Commentaires et documentation en français.
