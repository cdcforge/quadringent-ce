"""Routes ``/v2/auth/oidc/*`` — optionnelles, montées seulement si ``oidc_config`` est déclarée (tâche 10).

``login`` redirige vers le fournisseur d'identité (Authorization Code +
PKCE) ; ``callback`` échange le code, vérifie le jeton d'identité, résout
l'utilisateur (``UsersService.link_or_create_oidc_user``) et pose
**exactement** le même cookie de session que ``/v2/auth/login`` (mot de
passe) — voir ``oidc.py`` pour le détail du flux.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from ..errors import invalid_request
from ..oidc import OidcError, OidcService
from ..services.users import UserValidationError, UsersService

router = APIRouter(prefix="/v2/auth/oidc", tags=["oidc"])

_STATE_COOKIE_NAME = "quadringent_oidc_state"


def _oidc_service(request: Request) -> OidcService:
    service = getattr(request.app.state, "oidc_service", None)
    if service is None:
        raise invalid_request("OIDC non configuré sur ce control plane")
    return service


def _users_service(request: Request) -> UsersService:
    return UsersService(
        request.app.state.engine, org_id=request.app.state.org_id, pepper=request.app.state.token_pepper
    )


@router.get("/login")
async def oidc_login(request: Request, response: Response) -> RedirectResponse:
    service = _oidc_service(request)
    try:
        authorization_url, state_cookie_value = service.build_authorization_request()
    except OidcError as error:
        raise invalid_request(str(error)) from error
    redirect = RedirectResponse(authorization_url, status_code=302)
    redirect.set_cookie(
        _STATE_COOKIE_NAME,
        state_cookie_value,
        httponly=True,
        samesite="lax",
        secure=bool(getattr(request.app.state, "session_cookie_secure", False)),
        max_age=600,
    )
    return redirect


@router.get("/callback")
async def oidc_callback(request: Request, response: Response, code: str, state: str) -> dict[str, object]:
    service = _oidc_service(request)
    users_service = _users_service(request)
    state_cookie_value = request.cookies.get(_STATE_COOKIE_NAME)
    try:
        claims = service.complete_callback(code=code, state=state, state_cookie_value=state_cookie_value)
        user = users_service.link_or_create_oidc_user(oidc_subject=claims.subject, email=claims.email)
    except OidcError as error:
        raise invalid_request(str(error)) from error
    except UserValidationError as error:
        raise invalid_request(str(error)) from error

    session_token = users_service.create_session_token(user)
    response.delete_cookie(_STATE_COOKIE_NAME)
    response.set_cookie(
        request.app.state.session_cookie_name,
        session_token,
        httponly=True,
        samesite="strict",
        secure=bool(getattr(request.app.state, "session_cookie_secure", False)),
    )
    return {"user": user.to_dict()}
