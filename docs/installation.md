# Installation

Nécessite Python ≥ 3.10. On installe d'abord [pipx](https://pipx.pypa.io) (une seule fois),
qui rend la commande `myges` disponible partout.

## 1. pipx

**Linux (Debian / Ubuntu) et WSL**

```bash
sudo apt install pipx
pipx ensurepath
```

**Linux (Arch, Omarchy, Manjaro…)** : pas d'`apt` ici, c'est `pacman`. Si `uv` est déjà
installé (c'est le cas sur Omarchy), il remplace pipx : voir [Avec uv](#avec-uv-sans-pipx).

```bash
sudo pacman -S python-pipx
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

## Avec uv (sans pipx)

[uv](https://docs.astral.sh/uv/) fait la même chose que pipx. Pratique quand pipx n'est pas
disponible, par exemple sur Omarchy où `uv` est fourni d'office :

```bash
uv tool install "myges-api[cli]"
myges login
```

Si `myges` est introuvable ensuite : `uv tool update-shell`, puis ouvrir un nouveau terminal.

Équivalents des commandes pipx de cette page :

| pipx | uv |
|---|---|
| `pipx upgrade myges-api` | `uv tool upgrade myges-api` |
| `pipx inject myges-api google-auth` | `uv tool install --with google-auth "myges-api[cli]"` |
| `pipx uninstall myges-api` | `uv tool uninstall myges-api` |

## Mise à jour

```bash
pipx upgrade myges-api
myges --version
```

`myges` signale lui-même une nouvelle version (vérifié une fois par jour sur PyPI, jamais
dans les tâches planifiées) ; `MYGES_NO_UPDATE_CHECK=1` pour s'en passer.

**`pipx upgrade` répond « already at latest version » alors qu'une version plus récente
est sur PyPI ?** Juste après une publication, la liste des versions met jusqu'à
10 minutes à se mettre à jour (cache de pip, et parfois de PyPI lui-même). Réessayer un
peu plus tard ; `--pip-args=--no-cache-dir` écarte au moins le cache local :

```bash
pipx upgrade --pip-args=--no-cache-dir myges-api
```

Plus rare : une installation faite depuis GitHub, un zip ou avec une version fixée
(`myges-api==0.4.1`) reste bloquée sur sa source. Réinstaller une fois depuis PyPI :
`pipx install --force "myges-api[cli]"`.

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
