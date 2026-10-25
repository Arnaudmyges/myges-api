"""Menu interactif plein écran : ``myges`` seul dans un terminal, ou ``myges menu``.

Textual n'est importé qu'ici, au lancement du menu : les autres commandes (et les tâches
planifiées comme ``myges watch``) ne paient pas son temps de chargement.
"""

from __future__ import annotations


def run() -> int:
    """Lance le menu ; renvoie le code de sortie."""
    from .app import MygesApp

    app = MygesApp()
    app.run()
    return app.return_code or 0
