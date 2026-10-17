# Assistant : poser ses questions à Claude (MCP)

`myges mcp` est un serveur [MCP](https://modelcontextprotocol.io) : il donne à Claude
(Claude Code, Claude Desktop, ou tout client MCP) l'accès à votre espace myGES. On pose
ensuite ses questions en français :

- « Quelle est ma moyenne ? »
- « En quelle salle est mon prochain cours de Python ? »
- « Écris au prof de Linux que j'ai oublié de signer Edusign à son dernier cours. »
- « Qu'est-ce que je dois rendre cette semaine pour le projet annuel ? »
- « À quels événements je suis inscrit ? »

## Installation

Inclus dans l'installation habituelle (`pipx install "myges-api[cli]"`, voir
[installation](installation.md)) :

```bash
myges login              # si ce n'est pas déjà fait
myges mcp --install      # déclare le serveur dans Claude Code
```

Ouvrir ensuite une nouvelle session Claude Code (`claude`) et poser une question. Au
premier appel, Claude Code demande d'autoriser les outils `myges`.

**Claude Desktop** ou autre client : `myges mcp --install` sans Claude Code installé
affiche le bloc à ajouter à la configuration du client :

```json
{ "mcpServers": { "myges": { "command": "/chemin/vers/myges", "args": ["mcp"] } } }
```

Le serveur tourne sur votre machine (pas sur claude.ai web ni mobile, qui demandent un
serveur public sur Internet).

## Ce que Claude peut faire

33 outils. **Lecture** (rien ne change, ni sur myGES ni sur la machine) :

| Domaine | Outils |
| --- | --- |
| Profil, école | `whoami`, `classes`, `schools`, `campuses` (adresses), `partners` |
| Emploi du temps | `agenda`, `next_class` (salles, horaires, intervenant ; filtre par cours) |
| Notes | `grades` (moyennes par matière, par bloc, générale), `simulate_average` (« et si j'avais 14 en anglais ? »), `grade_needed` (« quelle note pour avoir 12 ? »), `absences` |
| Matières | `courses`, `syllabus`, `course_files` (supports déposés) |
| Personnes | `teachers` (avec leurs matières), `classmates` |
| Projets, TP | `projects`, `practicals`, `next_deadlines`, `project_messages` |
| Vie de campus | `events`, `pending_registrations`, `news`, `read_news`, `announcements` |
| Récapitulatif | `weekly_summary` (le même que `myges summary`) |

**Fichiers sur la machine** (rien ne change sur myGES) : `download_course_files`,
`download_project_files` (dans `~/myges` par défaut), `export_agenda_ics`,
`cancel_pending_registration`, et `draft_email` : un **brouillon** dans Outlook sur le
web, rien ne part sans votre clic sur « Envoyer » (sans navigateur, Claude donne le lien).

**Votre compte myGES** : `subscribe_event` (tout de suite si c'est ouvert, sinon
pré-inscription à l'ouverture, en arrière-plan) et `unsubscribe_event`. Ces deux outils
sont marqués « destructifs » : le client MCP demande votre accord avant chaque appel.
Évitez de les autoriser « toujours ».

Pas d'outil pour écrire dans la messagerie d'un groupe de projet, ni pour rejoindre ou
quitter un groupe : ces actions de l'API n'ont pas encore été vérifiées en conditions
réelles.

## Vie privée

Les réponses des outils (notes, absences, e-mails de vos camarades…) sont envoyées au
modèle d'IA de votre client MCP, comme tout ce que vous lui collez. `whoami` ne renvoie
ni adresse postale, ni date de naissance, ni INE. Le serveur utilise la session de
`myges login`, et ne transmet jamais votre mot de passe.
