"""Routes ``/v2/setup/first-admin``, ``/v2/users``, ``/v2/auth`` (tâche 9).

Le premier admin s'active hors authentification (``/v2/setup/first-admin``
n'exige aucun scope — il échoue simplement si un admin actif existe déjà) ;
les invitations suivantes exigent le scope ``admin``. La connexion par mot
de passe pose un cookie de session ``HttpOnly``/``SameSite=Strict``
(``Secure`` configurable via ``app.state.session_cookie_secure`` — désactivé
par défaut pour permettre les tests/`localhost` en HTTP, activé en
production par la chart).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ..auth import require_scope
from ..errors import ApiError, invalid_request, not_found
from ..http import AuditContext, envelope, idempotent_write
from ..services.users import (
    ActivationTokenInvalidError,
    ActivationTargetMismatchError,
    ActivationReissueForbiddenError,
    AdminAlreadyExistsError,
    InvalidCredentialsError,
    UserNotFoundError,
    UsersService,
    UserValidationError,
)

router = APIRouter(tags=["users"])


def _service(request: Request) -> UsersService:
    return UsersService(
        request.app.state.engine,
        org_id=request.app.state.org_id,
        pepper=request.app.state.token_pepper,
    )


@router.post("/v2/setup/first-admin", status_code=201)
async def create_first_admin(request: Request, response: Response) -> dict[str, object]:
    service = _service(request)
    path = "/v2/setup/first-admin"

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        try:
            record, activation_token = service.create_first_admin(email=payload.get("email"))
        except UserValidationError as error:
            raise invalid_request(str(error)) from error
        except AdminAlreadyExistsError as error:
            raise ApiError(
                409,
                "capability_unavailable",
                str(error),
                next_action="se connecter avec le compte admin existant",
                retryable=False,
            ) from error
        body = record.to_dict()
        body["activation_token"] = activation_token
        return 201, envelope(before=None, after=body, verify_method="GET", verify_path=f"/v2/users/{record.id}")

    return await idempotent_write(
        request=request,
        response=response,
        actor_id="setup",
        method="POST",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
    )


@router.get("/v2/users/{user_id}")
async def get_user(user_id: str, request: Request, identity=Depends(require_scope("admin"))) -> dict[str, object]:
    service = _service(request)
    try:
        record = service.get(user_id)
    except UserNotFoundError as error:
        raise not_found("utilisateur introuvable") from error
    return record.to_dict()


@router.get("/v2/users")
async def list_users(request: Request, identity=Depends(require_scope("admin"))) -> dict[str, object]:
    service = _service(request)
    return {"items": [record.to_dict() for record in service.list()], "next_cursor": None}


@router.post("/v2/users", status_code=201)
async def invite_user(
    request: Request, response: Response, identity=Depends(require_scope("admin"))
) -> dict[str, object]:
    service = _service(request)

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        try:
            record, activation_token = service.invite(email=payload.get("email"), role=payload.get("role"))
        except UserValidationError as error:
            raise invalid_request(str(error)) from error
        body = record.to_dict()
        body["activation_token"] = activation_token
        return 201, envelope(before=None, after=body, verify_method="GET", verify_path=f"/v2/users/{record.id}")

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path="/v2/users",
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action="user.invite", resource_type="user"),
        identity=identity,
    )


@router.post("/v2/users/{user_id}/activation/reissue", status_code=201)
async def reissue_first_admin_activation(
    user_id: str, request: Request, response: Response, identity=Depends(require_scope("admin"))
) -> dict[str, object]:
    service = _service(request)
    path = f"/v2/users/{user_id}/activation/reissue"

    def build(_payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        try:
            record, token = service.reissue_first_admin_activation(user_id)
        except UserNotFoundError as error:
            raise not_found("utilisateur introuvable dans cette organisation") from error
        except ActivationReissueForbiddenError as error:
            raise ApiError(
                409,
                "capability_unavailable",
                str(error),
                next_action="relire l'état du premier administrateur",
                retryable=False,
            ) from error
        return 201, envelope(
            before=None,
            after={**record.to_dict(), "activation_token": token},
            verify_method="GET",
            verify_path=f"/v2/users/{user_id}",
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action="user.activation.reissue", resource_type="user", resource_id=user_id),
        identity=identity,
    )


@router.post("/v2/users/activate")
async def activate_user_by_token(request: Request, response: Response) -> dict[str, object]:
    """Activation par le seul jeton : c'est la forme du lien remis par
    l'installeur (``/activate?token=…``), qui ne connaît pas l'identifiant."""
    service = _service(request)
    path = "/v2/users/activate"

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        activation_token = payload.get("token", payload.get("activation_token"))
        password = payload.get("password")
        if not isinstance(activation_token, str) or not isinstance(password, str):
            raise invalid_request("token et password requis")
        try:
            record = service.activate(activation_token=activation_token, password=password)
        except ActivationTokenInvalidError as error:
            raise invalid_request(str(error)) from error
        return 200, envelope(
            before=None, after=record.to_dict(), verify_method="GET", verify_path=f"/v2/users/{record.id}"
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id="activation",
        method="POST",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
    )


@router.post("/v2/users/{user_id}/activate")
async def activate_user(user_id: str, request: Request, response: Response) -> dict[str, object]:
    service = _service(request)
    path = f"/v2/users/{user_id}/activate"

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        activation_token = payload.get("activation_token")
        password = payload.get("password")
        if not isinstance(activation_token, str) or not isinstance(password, str):
            raise invalid_request("activation_token et password requis")
        try:
            record = service.activate(
                activation_token=activation_token, password=password, expected_user_id=user_id
            )
        except ActivationTargetMismatchError as error:
            raise not_found(str(error)) from error
        except ActivationTokenInvalidError as error:
            raise invalid_request(str(error)) from error
        return 200, envelope(
            before=None, after=record.to_dict(), verify_method="GET", verify_path=f"/v2/users/{user_id}"
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=user_id,
        method="POST",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
    )


@router.post("/v2/auth/login")
async def login(request: Request, response: Response) -> dict[str, object]:
    service = _service(request)
    payload = await request.json() if await request.body() else {}
    email = payload.get("email")
    password = payload.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        raise invalid_request("email et password requis")
    try:
        user = service.authenticate_password(email=email, password=password)
    except InvalidCredentialsError as error:
        from ..errors import invalid_credentials

        raise invalid_credentials(str(error)) from error
    session_token = service.create_session_token(user)
    response.set_cookie(
        request.app.state.session_cookie_name,
        session_token,
        httponly=True,
        samesite="strict",
        secure=bool(getattr(request.app.state, "session_cookie_secure", False)),
    )
    return {"user": user.to_dict()}


@router.post("/v2/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, object]:
    response.delete_cookie(request.app.state.session_cookie_name)
    return {"logged_out": True}


@router.get("/v2/auth/me")
async def get_current_identity(identity=Depends(require_scope("read"))) -> dict[str, object]:
    """Identité courante — pour que l'UI sache si elle est connectée (tâche
    « auth-login »). 401 (enveloppe standard) sans identité résolue ; en
    mode « authentification exigée », c'est le cas sans cookie de session
    (ni jeton d'agent ni proxy de confiance) valide."""

    return {
        "subject": identity.subject,
        "role": identity.role,
        "actor_kind": identity.actor_kind,
        "email": identity.actor_display if identity.actor_kind == "human" else None,
    }
