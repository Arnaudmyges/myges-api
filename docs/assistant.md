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

```bash
pipx install --force "myges-api[cli,mcp]"   # ajoute le module MCP
myges login                                 # si ce n'est pas déjà fait
myges mcp --install                         # déclare le serveur dans Claude Code
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

| Outil | Pour |
| --- | --- |
| `whoami` | nom, e-mail de l'école, classe, année en cours |
| `agenda`, `next_class` | cours, horaires, salles, campus, intervenant (filtre par nom de cours) |
| `grades` | notes, moyenne par matière, par bloc (`myges blocs`) et générale |
| `absences` | absences et retards |
| `courses`, `teachers` | matières, coefficients, intervenants et leurs e-mails |
| `classmates` | camarades de classe et leurs e-mails |
| `projects` | votre groupe, ses membres, étapes, consignes, échéances |
| `events` | événements du campus, inscriptions |
| `news`, `read_news` | actualités de l'école |
| `draft_email` | **brouillon** d'e-mail dans Outlook |

Tout est en **lecture seule**. `draft_email` ouvre un brouillon dans Outlook sur le web
(destinataire, objet et texte remplis) : rien ne part sans votre clic sur « Envoyer ».
Sur une machine sans navigateur (SSH), Claude donne le lien du brouillon.

Claude ne peut ni s'inscrire à un événement, ni déposer un fichier, ni rien modifier sur
myGES : pour ça, les commandes `myges events subscribe` etc. restent à lancer vous-même.

## Vie privée

Les réponses des outils (notes, absences, e-mails de vos camarades…) sont envoyées au
modèle d'IA de votre client MCP, comme tout ce que vous lui collez. `whoami` ne renvoie
ni adresse postale, ni date de naissance, ni INE. Le serveur utilise la session de
`myges login`, et ne transmet jamais votre mot de passe.
