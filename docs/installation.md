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

**`pipx upgrade` répond « already at latest version » avec une ancienne version ?**
pipx réinstalle depuis la source d'origine : une installation faite depuis GitHub, un
zip ou avec une version fixée (`myges-api==0.4.1`) reste bloquée. Réinstaller une fois
depuis PyPI, sans version ; ensuite `pipx upgrade` suffit :

```bash
deactivate 2>/dev/null                       # si un environnement virtuel est activé
pipx install --force "myges-api[cli]"        # ou "myges-api[cli,mcp]" pour l'assistant
myges --version
```

Pour voir la source d'origine : `grep package_or_url
~/.local/share/pipx/venvs/myges-api/pipx_metadata.json`.

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
