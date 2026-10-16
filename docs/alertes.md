# Alertes : nouvelles notes, supports, cours annulés

`myges watch` compare l'état actuel de myGES à celui de son passage précédent et signale :

- les **nouveaux supports de cours**, vérifiés toutes les 5 minutes ;
- les nouvelles notes, notes d'examen et moyennes modifiées ;
- les nouvelles absences ;
- les cours ajoutés, déplacés ou annulés dans les 14 prochains jours.

Notes, absences et agenda sont vérifiés toutes les 30 minutes : c'est ce qui permet de
surveiller les supports de près sans surcharger myGES.

Le premier passage enregistre seulement l'état actuel.

## 1. Choisir où recevoir les alertes

```bash
myges notify add ntfy                       # notifications sur téléphone
myges notify add discord <webhook>          # salon Discord
myges notify add email moi@exemple.fr --smtp smtp.gmail.com --user moi@gmail.com
myges notify test                           # vérifier que ça arrive
myges notify list / remove <n°>
```

**ntfy** : installer l'appli [ntfy](https://ntfy.sh) (Android, iOS ou web) et s'abonner au
sujet affiché par `myges notify add ntfy`. Ce sujet est aléatoire exprès : sur ntfy.sh,
quiconque connaît le sujet peut lire les messages.

**Discord** : dans un salon, **Paramètres du salon → Intégrations → Webhooks → Nouveau
webhook → Copier l'URL**. Préférer un serveur ou un salon privé.

**E-mail** : le mot de passe SMTP est demandé puis stocké dans `~/.config/myges/`, lisible par
vous seul. Gmail : activer la validation en deux étapes et créer un
[mot de passe d'application](https://myaccount.google.com/apppasswords).

## 2. Activer

```bash
myges login --save-password      # la session se renouvelle sans vous
myges watch --install            # lancement automatique (toutes les 5 min)
myges watch --status             # vérifier que tout est en place
```

`--install` ajoute une ligne à votre crontab (Linux, macOS, WSL) ou une tâche planifiée
(Windows), et signale ce qui empêcherait les alertes d'arriver : aucun canal, mot de passe
non enregistré, cron arrêté. Sous **WSL**, cron ne démarre pas tout seul :
`sudo service cron start`.

Arrêter : `myges watch --uninstall`.

Sans `--save-password`, les alertes s'arrêtent à l'expiration de la session (~6 jours) : une
seule alerte vous demande alors de relancer `myges login`.

## Si un envoi échoue

- Un canal indisponible (quota ntfy, Discord en panne…) ne fait rien perdre : les nouveautés
  sont renvoyées au passage suivant.
- Si myGES est injoignable plus de 2 heures, une alerte le signale, puis une autre quand
  c'est rétabli.
- Une pré-inscription à un événement interrompue par un arrêt de la machine est relancée
  automatiquement.

## Options

```bash
myges watch                      # vérifier tout de suite, à la main
myges watch --no-notify          # afficher sans envoyer
myges watch --quiet              # rien afficher s'il n'y a rien de nouveau
```

## Données personnelles

Les alertes contiennent des notes et des absences. Elles ne transitent que par les canaux que
vous configurez (ntfy.sh, Discord, votre serveur SMTP) ; garder un sujet ntfy aléatoire et un
salon Discord privé.
