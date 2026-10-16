# Commandes

`myges --help` liste tout ; `myges <commande> --help` détaille chaque commande. Toutes les
commandes de consultation acceptent `--json` (sortie brute) et, quand c'est pertinent,
`-y 2025` pour une autre année scolaire.

## Session

```bash
myges login                      # identifiant + mot de passe ; le compte est retenu
myges login --save-password      # retenir aussi le mot de passe (tâches planifiées)
myges logout
myges token                      # token à coller dans « Authorize » du Swagger
```

La session dure ~6 jours ; ensuite, relancer `myges login`.

## Emploi du temps

```bash
myges next                       # cours en cours ou prochain : délai, salle, intervenant
myges today                      # cours du jour
myges agenda                     # 7 prochains jours (--days 14, --from / --to AAAA-MM-JJ)
myges agenda --ics semaine.ics   # export iCalendar
```

Les heures sont affichées à l'heure de Paris (variable `MYGES_TZ` pour changer).

## Matières, notes, supports

```bash
myges courses                    # matières, numérotées (colonne #)
myges grades                     # notes
myges absences
myges files                      # supports de cours de l'année
myges files 3                    # ceux d'une matière
myges files --download           # tout télécharger dans le dossier courant
myges files --download ~/cours   # … ou ailleurs ; relancer ne récupère que les nouveaux
myges syllabus anglais
```

Une matière se désigne par son **numéro `#`** (affiché dans `courses`, `grades`, `files`,
`agenda`, `projects`…), **une partie de son nom** (`anglais`, `"algo python"`, casse et
accents ignorés) ou son **`rc_id`**. Si plusieurs matières correspondent, la commande les
liste au lieu de deviner. Le numéro peut changer si une matière est ajoutée en cours d'année :
préférer le nom dans les scripts.

## Projets

```bash
myges projects
myges practicals                 # TP
myges steps                      # prochaines échéances
```

## Blocs et simulation de notes

myGES ne fournit pas les blocs de compétences : on les définit une fois (par année), puis la
CLI calcule la moyenne de chaque bloc et la moyenne générale.

```bash
myges blocs add "Bloc 1" 1 3 anglais      # matières par numéro, nom ou rc_id
myges blocs                               # moyenne par bloc (validé si ≥ 10) et générale
myges blocs -s 3=15 -s anglais=12         # et si j'avais 15 en #3 ?
myges blocs remove "Bloc 1" 3             # retirer une matière (sans matière : tout le bloc)
```

- Pondération : coefficient de la matière, ou 1 tant qu'il n'est pas publié (signalé `¹`).
- Moyenne d'une matière : l'officielle, sinon la moyenne de contrôle continu (signalée `~`,
  provisoire), sinon la matière n'est pas comptée. Une note simulée est signalée `*`.

## Événements : s'inscrire dès l'ouverture

Les places partent vite : `subscribe` attend l'heure d'ouverture des inscriptions et
s'inscrit à la seconde près.

```bash
myges events                     # événements à venir : id, ouverture des inscriptions, inscrit ?
myges events subscribe 4525 4529 # inscrit tout de suite, ou attend l'ouverture
myges events unsubscribe 4525
```

L'`id` est aussi le nombre à la fin de l'adresse de l'événement sur myGES
(`myges.fr/common/event/4525`). En attente de l'ouverture, laisser le terminal ouvert (ou
utiliser `tmux` sur un serveur). L'heure suivie est celle du serveur myGES, pas celle de la
machine.

## Vie de l'école et personnes

```bash
myges news                       # actualités
myges campus
myges teachers
myges classes
myges students <puid>            # étudiants d'une classe (puid donné par `myges classes`)
myges profile
```

## Alertes et Google Calendar

- [Alertes](alertes.md) : `myges watch`, `myges notify …`
- [Google Calendar](google-calendar.md) : `myges gcal …`

## Exploration de l'API

```bash
myges raw /me/2026/courses               # GET brut sur n'importe quel chemin
myges raw /me/news -p size=5             # avec paramètres
```
