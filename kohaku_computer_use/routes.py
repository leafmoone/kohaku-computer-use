"""Same-origin operator endpoints mounted by the package UI slot."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from kohakuterrarium.api.deps import get_service
from kohakuterrarium.terrarium.service.service import LocalTerrariumService
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .broker import BROKER, OPERATOR, BrokerError, DesktopBroker
from .models import Mode, RootItem, StateDetail, StateIndex, StatusPayload

router = APIRouter()


class OperatorBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


Pattern = Annotated[str, Field(min_length=1, max_length=512)]


class PolicyBody(OperatorBody):
    mode: Mode | None = None
    allow_foreground_input: bool | None = None
    idle_timeout_s: Annotated[int, Field(ge=0, le=86400)] | None = None
    hotkey: Annotated[str, Field(min_length=1, max_length=60)] | None = None
    denylist_patterns: Annotated[list[Pattern], Field(max_length=100)] | None = None
    allowlist_patterns: Annotated[list[Pattern], Field(max_length=100)] | None = None

    @model_validator(mode='after')
    def reject_explicit_null(self):
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError('Policy values cannot be null')
        return self


class PanicBody(OperatorBody):
    reason: Annotated[str, Field(min_length=1, max_length=512)]


class ObserveBody(OperatorBody):
    root_ref: Annotated[str, Field(pattern=r'^@r[1-9][0-9]*$', max_length=16)] | None = None
    hwnd: Annotated[int, Field(ge=1, le=2**63 - 1)] | None = None
    include_image: bool = False


def local_broker(service: Annotated[object, Depends(get_service)]) -> DesktopBroker:
    if not isinstance(service, LocalTerrariumService):
        raise HTTPException(status_code=501, detail='A local Terrarium service is required')
    return BROKER


Desktop = Annotated[DesktopBroker, Depends(local_broker)]


def http_error(exc: BrokerError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.get('/status')
def status(broker: Desktop) -> StatusPayload:
    return broker.status()


@router.post('/policy')
def policy(body: PolicyBody, broker: Desktop) -> StatusPayload:
    try:
        return broker.change_policy(body.model_dump(exclude_unset=True))
    except BrokerError as exc:
        raise http_error(exc) from exc


@router.post('/panic')
def panic(body: PanicBody, broker: Desktop) -> StatusPayload:
    return broker.panic(body.reason, OPERATOR)


@router.post('/panic/reset')
def reset(broker: Desktop, _body: OperatorBody | None = None) -> StatusPayload:
    try:
        return broker.reset_panic()
    except BrokerError as exc:
        raise http_error(exc) from exc


@router.get('/roots')
def roots(broker: Desktop) -> dict[str, list[RootItem]]:
    try:
        return {'roots': broker.list_roots(None)}
    except BrokerError as exc:
        raise http_error(exc) from exc


@router.post('/observe')
def observe(body: ObserveBody, broker: Desktop) -> StateDetail:
    try:
        detail, _ = broker.observe(None, body.root_ref, body.hwnd, body.include_image, 150)
        return detail
    except BrokerError as exc:
        raise http_error(exc) from exc


@router.get('/states')
def states(broker: Desktop) -> StateIndex:
    return broker.state_index()


@router.get('/state/{state_id}')
def state(state_id: str, broker: Desktop) -> StateDetail:
    try:
        return broker.state(state_id)
    except BrokerError as exc:
        raise http_error(exc) from exc


@router.get('/frame/{state_id}', response_class=Response)
def frame(state_id: str, broker: Desktop) -> Response:
    try:
        return Response(broker.frame(state_id), media_type='image/png', headers={'Cache-Control': 'no-store'})
    except BrokerError as exc:
        raise http_error(exc) from exc
