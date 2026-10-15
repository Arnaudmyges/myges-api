# Notes sur l'API myGES / Kordis

Comportements observés sur l'API réelle, consignés au fil des découvertes. La référence
exhaustive reste la [spécification](../openapi/myges.yaml) ; ce fichier garde le *pourquoi* et
le *quand*.

Dans la spec, chaque opération porte `x-verified` : la date de la dernière vérification
sur l'API réelle, ou `false` pour une route jamais appelée (les écritures).

Chaque entrée est datée. Si un comportement change, ajouter une nouvelle entrée plutôt que
réécrire l'ancienne.

## 2026-10-15 — Exploration initiale

### Authentification

- OAuth 2.0 *implicit grant* détourné : identifiants en Basic Auth sur
  `https://authentication.kordis.fr/oauth/authorize?response_type=token&client_id=skolae-app`,
  réponse `302` vers `comreseaugesskolae:/oauth2redirect#access_token=…`. Le token est dans le
  **fragment** de `Location` ; ne pas suivre la redirection.
- Durée de vie ≈ 6,5 jours (`expires_in` ≈ 560 000 s), **pas de refresh token**.
- Tant qu'un token est actif, une nouvelle authentification renvoie **le même token**, avec un
  `expires_in` réduit : se ré-authentifier ne prolonge rien.
- Identifiants invalides : `401` en HTML, `WWW-Authenticate: Basic`.

### Pare-feu

- `403` (page HTML) pour le User-Agent `python-requests/*`, sur les deux domaines, et pour une
  requête sans User-Agent sur `authentication.kordis.fr`. `curl`, `python-httpx` ou un
  User-Agent explicite passent. Le client envoie `myges-api/<version>`.

### Format des réponses

- Enveloppe commune : `{"response_code": 0, "version": "1", "result": …, "links": []}`.
  `response_code` vaut `1` sur les `400`, et `0` sur les `404`.
- Dates en **millisecondes** epoch.
- `401` de l'API en **XML** (`<InvalidTokenException>`…), autres erreurs en JSON façon Spring
  Boot (`timestamp`, `status`, `error`, `message`, `path`).
- `204` sans corps au lieu d'une liste vide (absences sans absence, année inconnue…).
- HAL incohérent : `links` (tableau) dans les éléments de liste, `_links` (objet) dans les
  ressources unitaires (`application/hal+json`).
- Types instables : `coef`/`ects` sont des nombres dans `/me/{year}/courses` mais des chaînes
  (`"3.0"`, `"N.C."`) dans `/me/{year}/grades`.
- Champs pouvant valoir `null` alors qu'on les voit presque toujours remplis : `rooms`
  (cours à distance), `steps`, `project_files`, `project_group_logs`, `psp_limit_date`. Repérés
  en validant les trois années disponibles (2024 à 2026), pas une seule.
- CORS : l'API renvoie l'origine appelante pour les `GET`, d'où le « Try it out » fonctionnel
  dans la doc Swagger.

### Routes `/me/{x}/…` : année ou matière

Certaines routes prennent une **année** (`/me/2026/grades`), d'autres un **`rc_id`** de matière
(`/me/{rc_id}/files`, `/me/{rc_id}/syllabus`), sous la même forme de chemin. Une mauvaise
valeur ne produit pas d'erreur claire :

- `/me/2026/syllabus` → `200 []` (2026 lu comme un `rc_id`) ;
- `/me/2026/files` → `400` « Vous n'avez pas accès à cette ressource » ;
- `/me/{rc_id}/grades` → `204` (rc_id lu comme une année).

C'est ce qui avait fait conclure à tort que le syllabus était toujours vide.

### Supports de cours

Point de départ : la page web `myges.fr/student/courses-files`, dont les liens pointent vers
`ges-dl.kordis.fr/private/<jeton>`. Ces liens exigent une **session web CAS** : le Bearer de
l'API n'est pas accepté.

En sondant l'API sur le modèle `/me/{rc_id}/…` :

- `GET /me/{rc_id}/files` liste les supports. La liste est non vide exactement quand
  `has_documents` vaut `true` (vérifié sur 82 matières).
- `GET /me/{rc_id}/files/{oc_id}` renvoie le **fichier brut** avec le token de l'API,
  identique octet pour octet au téléchargement web (vérifié sur 6 formats). Le serveur annonce
  `Content-Type: application/json` quel que soit le format, sans `Content-Disposition`.
- `oc_id` inconnu → `500` ; matière d'un autre étudiant → `400`.

### Autres routes trouvées

- `GET /me/{rc_id}/syllabus` : fiche complète (plan par séance, compétences, évaluation).
- `GET /me/{year}/practicals` : TP, même structure que les projets (`project_type_id = 2`).
- `GET /me/schools`, `GET /me/campus`.
- `POST /me/events/{id}/subscribe` : existence déduite d'un `405` (`Allow: POST`) en réponse à
  un `GET`. Jamais appelée.

### Inaccessible à un compte étudiant

| Route | Réponse |
|---|---|
| `GET /me/{year}/students` | `418` « Cette ressource n'est pas utilisable par ce rôle utilisateur » |
| `GET /me/campus/{id}/rooms` | `418`, même message |
| `/planning/campus…`, `/planning/rooms` (cibles des liens de `/me/campus`) | `401` JSON : autre scope que `account` |

### Uniquement sur le site web

Documents administratifs (certificats de scolarité, admission), factures et points OPEN :
présents sur `myges.fr` (JSF + CAS), aucun équivalent trouvé dans l'API parmi ~400 chemins
testés en `GET`.
