"""Mini-serveur HTTP pour agir depuis une notification (bouton « S'inscrire »).

Les notifications ntfy peuvent porter des boutons qui envoient une requête HTTP. Ce
serveur ne sait faire qu'une chose : recevoir ``POST /events/<id>/subscribe`` et lancer
la pré-inscription correspondante. Il est fait pour écouter sur une adresse privée
(tailnet, réseau local), jamais sur Internet, et exige un jeton à chaque requête.

Routes :
    POST /events/<id>/subscribe   pré-inscription (ou inscription immédiate si ouvert)
    GET  /health                  « ok », pour les sondes
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("myges.serve")

SUBSCRIBE = re.compile(r"^/events/(\d{1,12})/subscribe/?$")


def make_server(
    host: str, port: int, token: str, on_subscribe: Callable[[int], str]
) -> ThreadingHTTPServer:
    """Serveur prêt à ``serve_forever()``.

    ``on_subscribe(id)`` renvoie un message lisible ; ``LookupError`` → 404 (événement
    inconnu), toute autre exception → 500.
    """
    expected = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "myges-serve"

        def _reply(self, status: int, payload: dict[str, str]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            given = self.headers.get("Authorization", "").encode()
            return hmac.compare_digest(given, expected)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._reply(200, {"status": "ok"})
            else:
                self._reply(404, {"error": "route inconnue"})

        def do_POST(self) -> None:
            match = SUBSCRIBE.match(self.path)
            if match is None:
                self._reply(404, {"error": "route inconnue"})
                return
            if not self._authorized():
                self._reply(401, {"error": "jeton absent ou invalide"})
                return
            event_id = int(match.group(1))
            try:
                message = on_subscribe(event_id)
            except LookupError as exc:
                self._reply(404, {"error": str(exc)})
                return
            except Exception as exc:  # la requête échoue proprement, le serveur continue
                log.warning("pré-inscription %s : %s", event_id, exc)
                self._reply(500, {"error": str(exc)})
                return
            log.info("pré-inscription demandée : %s", event_id)
            self._reply(202, {"status": "accepted", "message": message})

        def log_message(self, format: str, *args: object) -> None:
            log.debug("%s %s", self.address_string(), format % args)

    return ThreadingHTTPServer((host, port), Handler)
