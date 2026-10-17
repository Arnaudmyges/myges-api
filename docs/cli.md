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
myges projects                              # mon groupe et la prochaine échéance
myges projects --inspect 24134              # (id ou nom) groupe, membres, consignes, pièces jointes
myges projects --inspect proxmox --download # + télécharger les pièces jointes
myges practicals                            # TP, mêmes options
myges steps                      # prochaines échéances
```

## Blocs et simulation de notes

myGES ne fournit pas les blocs de compétences : on les définit une fois (par année), puis la
CLI calcule la moyenne de chaque bloc et la moyenne générale.

```bash
myges blocs setup                         # ranger chaque matière dans son bloc, une par une
myges blocs add "Bloc 1" 1 3 anglais      # ou directement : matières par numéro, nom ou rc_id
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
myges events                        # événements à venir : id, ouverture, inscrit / pré-inscrit
myges events --subscribed           # seulement ceux où je suis inscrit (--not-subscribed : l'inverse)
myges events subscribe 4525 4529    # s'inscrit à l'ouverture, en arrière-plan
myges events --inspect 4525         # description, intervenants, salle
myges events pending                # pré-inscriptions en attente
myges events cancel 4525            # annuler une pré-inscription (avant l'ouverture)
myges events unsubscribe 4525       # se désinscrire (après)
```

- L'`id` est aussi le nombre à la fin de l'adresse de l'événement : on peut coller
  l'adresse `myges.fr/common/event/4525` à la place.
- Chaque événement est tenté à **sa** propre heure d'ouverture, relue juste avant (si l'école
  la déplace, la nouvelle est suivie), selon l'horloge du serveur myGES.
- Si les inscriptions ne sont pas encore ouvertes, l'attente se fait en arrière-plan et la
  commande rend la main (`--wait` pour attendre dans le terminal). La mise en veille ne gêne
  pas ; un **arrêt de la machine** interrompt l'attente : `myges events` affiche alors
  « ⚠ interrompu », il suffit de relancer `subscribe`.
- Résultat envoyé sur vos canaux d'[alertes](alertes.md) s'il y en a.

## Vie de l'école et personnes

```bash
myges news                       # actualités (avec leur id)
myges news --inspect 4923        # lire une actualité : texte, liens (id ou partie du titre)
myges campus
myges teachers                   # intervenants et leurs matières
myges classes
myges students                   # votre classe (sinon : myges students <puid>)
myges profile
```

### Écrire à un intervenant ou à la classe

`--mail` ouvre un brouillon dans Outlook sur le web, destinataires remplis. Les adresses
myGES sont des boîtes Microsoft 365 de l'école : Outlook est déjà connecté au bon compte,
et rien ne part sans votre clic sur « Envoyer ».

```bash
myges teachers --mail python                      # par matière ou par nom
myges teachers --mail "martin, durand" -s "Projet annuel"
myges students --mail "dupont"                    # un camarade
myges students --mail tous --body "Salut à tous," # toute la classe, sauf vous
myges teachers --mail git --mailto                # client de messagerie par défaut
```

Un nom ambigu affiche les possibilités. Sans navigateur (SSH, VPS, WSL sans `wslview`),
le lien est affiché, à ouvrir sur une machine qui en a un.

## Alertes et Google Calendar

- [Alertes](alertes.md) : `myges watch`, `myges notify …`, rappels (`myges remind`),
  récapitulatif (`myges summary`), bouton « S'inscrire » (`myges notify serve`)
- [Google Calendar](google-calendar.md) : `myges gcal …`

## Assistant

- [Assistant (MCP)](assistant.md) : `myges mcp` pour poser ses questions à Claude,
  `myges mcp --install` pour l'ajouter à Claude Code

## Exploration de l'API

```bash
myges raw /me/2026/courses               # GET brut sur n'importe quel chemin
myges raw /me/news -p size=5             # avec paramètres
```
