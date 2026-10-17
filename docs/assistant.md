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

Pour Claude sur le **web** et le **mobile**, voir plus bas : connecteur distant.

## Ce que Claude peut faire

34 outils. **Lecture** (rien ne change, ni sur myGES ni sur la machine) :

| Domaine | Outils |
| --- | --- |
| Profil, école | `whoami`, `classes`, `schools`, `campuses` (adresses), `partners` |
| Emploi du temps | `agenda`, `next_class` (salles, horaires, intervenant ; filtre par cours) |
| Notes | `grades` (moyennes par matière, par bloc, générale), `simulate_average` (« et si j'avais 14 en anglais ? »), `grade_needed` (« quelle note pour avoir 12 ? »), `absences` |
| Matières | `courses`, `syllabus`, `course_files` (supports déposés) |
| Personnes | `teachers` (avec leurs matières), `classmates` |
| Projets, TP | `projects`, `practicals`, `next_deadlines`, `project_messages` (chat du groupe) |
| Vie de campus | `events`, `pending_registrations`, `news`, `read_news`, `announcements` |
| Récapitulatif | `weekly_summary` (le même que `myges summary`) |

**Fichiers sur la machine** (rien ne change sur myGES) : `download_course_files`,
`download_project_files` (dans `~/myges` par défaut), `export_agenda_ics`,
`cancel_pending_registration`, et `draft_email` : un **brouillon** dans Outlook sur le
web, rien ne part sans votre clic sur « Envoyer » (sans navigateur, Claude donne le lien).

**Votre compte myGES** : `subscribe_event` (tout de suite si c'est ouvert, sinon
pré-inscription à l'ouverture, en arrière-plan), `unsubscribe_event`, et
`send_project_message` (poster dans le chat de votre groupe de projet, visible par tout le
groupe). Ces outils sont marqués « destructifs » : le client MCP demande votre accord avant
chaque appel. Évitez de les autoriser « toujours ».

Pas d'outil pour rejoindre ou quitter un groupe de projet : ces actions de l'API n'ont pas
encore été vérifiées en conditions réelles.

## Claude sur le web et le mobile (connecteur distant)

claude.ai appelle ses connecteurs depuis les serveurs d'Anthropic : il faut un serveur
joignable en HTTPS public, par exemple un VPS avec
[Tailscale Funnel](https://tailscale.com/kb/1223/funnel) (aucun port à ouvrir) :

```bash
myges login --save-password             # sur le serveur
myges mcp --http 127.0.0.1:8765 --public-url https://mon-vps.tailnet.ts.net   # (service)
sudo tailscale funnel --bg 8765         # expose https://mon-vps.tailnet.ts.net
```

Puis dans claude.ai : **Paramètres → Connecteurs → Ajouter un connecteur personnalisé**,
adresse `https://mon-vps.tailnet.ts.net/mcp`. Claude ouvre une page de connexion servie par
votre serveur : identifiant et mot de passe **myGES** du compte configuré sur le serveur.
Le connecteur marche ensuite aussi dans l'appli mobile et Claude Desktop.

Ce que le serveur distant protège :

- **Connexion OAuth** : seul le compte myGES du serveur peut se connecter ; 5 échecs en
  15 minutes bloquent la page 15 minutes.
- **Adresses de retour** limitées à claude.ai et claude.com : un tiers ne peut pas
  récupérer d'accès chez lui. La page de connexion rappelle qui demande l'accès : ne la
  remplir que si vous venez de cliquer sur « Connecter ».
- **Jetons** valables 1 h (renouvellement 30 jours, à usage unique), stockés hachés.
  `myges mcp --revoke` déconnecte tout.
- **Outils** : 27, lecture et brouillons d'e-mail seulement. Pas d'inscription, pas de
  message de groupe, pas de téléchargement : pour ça, Claude Code ou Claude Desktop.

## Vie privée

Les réponses des outils (notes, absences, e-mails de vos camarades…) sont envoyées au
modèle d'IA de votre client MCP, comme tout ce que vous lui collez. `whoami` ne renvoie
ni adresse postale, ni date de naissance, ni INE. Le serveur utilise la session de
`myges login`, et ne transmet jamais votre mot de passe.
