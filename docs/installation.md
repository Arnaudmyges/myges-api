# Installation

Nécessite Python ≥ 3.10. On installe d'abord [pipx](https://pipx.pypa.io) (une seule fois),
qui rend la commande `myges` disponible partout.

## 1. pipx

**Linux (Debian / Ubuntu) et WSL**

```bash
sudo apt install pipx
pipx ensurepath
```

**macOS**

```bash
brew install pipx
pipx ensurepath
```

**Windows** : le plus simple est WSL (section Linux ci-dessus). Sinon, en natif (PowerShell,
avec Python depuis [python.org](https://www.python.org/downloads/) ou le Microsoft Store) :

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

## 2. myges

Dans un **nouveau terminal** :

```bash
pipx install "myges-api[cli]"
myges login
```

## Mise à jour

```bash
pipx upgrade myges-api
myges --version
```

`myges` signale lui-même une nouvelle version (vérifié une fois par jour sur PyPI, jamais
dans les tâches planifiées) ; `MYGES_NO_UPDATE_CHECK=1` pour s'en passer.

Installé avant la publication sur PyPI (depuis l'adresse GitHub) ? Une seule fois :
`pipx install --force "myges-api[cli]"`, puis `pipx upgrade` suffit.

## Complétion avec Tab

Activée automatiquement au premier `myges login` (effective dans les nouveaux terminaux).
Pour la refaire à la main : `myges --install-completion`.

## WSL : retrouver les supports dans l'Explorateur Windows

Télécharger vers le disque Windows plutôt que dans le dossier Linux :

```bash
myges files --download /mnt/c/Users/<toi>/Documents/cours
```

## Désinstaller

```bash
pipx uninstall myges-api
rm -rf ~/.config/myges ~/.cache/myges ~/.local/state/myges   # compte, session, état
```
