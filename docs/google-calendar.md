# Synchroniser myGES avec Google Calendar

`myges gcal sync` recopie l'emploi du temps dans un agenda Google Calendar dédié, et le tient à jour :
cours ajoutés, déplacés, annulés. Configuration : ~10 minutes, une seule fois.

## Pourquoi un compte de service

La synchro doit tourner sans vous (tâche planifiée). Avec une connexion Google classique
(OAuth), Google fait expirer l'autorisation au bout de 7 jours tant que votre projet Google
Cloud est en mode « Test », ce qui est le cas d'un projet personnel. Un **compte de service**
n'a pas ce problème, et ne voit **que** l'agenda que vous partagez avec lui, jamais votre
agenda principal.

## 1. Projet Google Cloud et API

1. Sur [console.cloud.google.com](https://console.cloud.google.com/), créer un projet.
2. **APIs & Services → Library** : chercher **Google Calendar API**, cliquer **Enable**.

## 2. Compte de service et clé

1. **IAM & Admin → Service Accounts → Create Service Account**. Nom libre (ex.
   `myges-sync`), aucun rôle nécessaire.
2. Sur le compte créé : **Keys → Add Key → Create new key → JSON**. Le fichier se télécharge ;
   c'est la seule copie.
3. Noter l'adresse du compte (`myges-sync@votre-projet.iam.gserviceaccount.com`).

## 3. Agenda dédié et partage

1. Dans Google Calendar : **Autres agendas → + → Créer un agenda** (ex. « Cours myGES »).
2. **Paramètres et partage → Partager avec des personnes en particulier → Ajouter** :
   l'adresse du compte de service, permission **Apporter des modifications aux événements**.
3. **Intégrer l'agenda → ID de l'agenda** : `xxxx@group.calendar.google.com`.

## 4. Configurer la CLI

```bash
pipx inject myges-api google-auth        # dépendance de la synchro, une fois
myges gcal setup ~/Téléchargements/cle.json xxxx@group.calendar.google.com
myges gcal sync --dry-run                # voir ce qui serait fait
myges gcal sync
```

`setup` vérifie l'accès à l'agenda puis copie la clé dans `~/.config/myges/` (lisible par
vous seul). Le fichier téléchargé peut ensuite être supprimé.

## 5. Planifier

```
0 */6 * * * $HOME/.local/bin/myges gcal sync --quiet
```

Comme pour `myges watch`, `myges login --save-password` évite que la tâche s'arrête quand
la session myGES expire (~6 jours).

## Ce qui est modifié dans l'agenda

- Seuls les événements créés par `myges gcal sync` sont touchés (repérés par une propriété
  privée). Ajouter ses propres événements dans le même agenda ne pose pas de problème.
- Un cours annulé côté myGES est supprimé de l'agenda ; les cours passés ne sont jamais
  modifiés.
- Fenêtre : 30 jours par défaut (`--days` pour changer).
- Ne pas faire écrire deux outils de synchro différents dans le même agenda : chacun créerait
  ses propres événements, en double.
