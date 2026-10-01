"""Utilitaire de test : pont ``httpx.MockTransport`` (sync) -> app FastAPI ``/v2``.

``ApiClient`` (``quadringent/installer/api_client.py``) utilise
``httpx.Client`` (synchrone) — mais l'ASGI de FastAPI/Starlette est
asynchrone. Ce module fait le pont via ``fastapi.testclient.TestClient``
(déjà utilisé par toute la suite ``test_v2_*``) : chaque requête
``httpx.MockTransport`` est rejouée sur ``TestClient(app)`` et sa réponse
traduite en ``httpx.Response``. Aucun réseau réel — conforme à la consigne
« Tests with httpx MockTransport / FastAPI TestClient; no network ».
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient


def asgi_app_transport(app) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        with TestClient(app) as test_client:
            response = test_client.request(
                request.method,
                str(request.url),
                content=request.content,
                headers=dict(request.headers),
            )
        return httpx.Response(response.status_code, headers=dict(response.headers), content=response.content)

    return httpx.MockTransport(handler)
