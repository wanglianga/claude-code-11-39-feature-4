"""API-key authentication with per-entity authorization scopes."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .db import get_db
from .util import key_hash

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="API key issued by an administrator")


class Principal:
    def __init__(self, user: m.User, entity_ids: set[int], entity_by_code: dict[str, m.Entity] | None = None):
        self.user = user
        self.entity_ids = entity_ids
        self.entity_by_code = entity_by_code or {}

    @property
    def name(self) -> str:
        return self.user.name

    @property
    def role(self) -> str:
        return self.user.role

    @property
    def is_admin(self) -> bool:
        return self.user.role == "admin"

    def can_write(self) -> bool:
        return self.user.role in ("admin", "analyst")

    def can_act_on(self, entity_id: int | None) -> bool:
        if self.is_admin:
            return True
        if entity_id is None:
            return False
        return entity_id in self.entity_ids

    def require_entity(self, entity_id: int | None) -> None:
        if not self.can_act_on(entity_id):
            raise HTTPException(403, "entity scope not granted to this principal")


def load_principal(db: Session, api_key: str) -> Principal:
    user = db.scalar(select(m.User).where(m.User.api_key_hash == key_hash(api_key), m.User.active.is_(True)))
    if user is None:
        raise HTTPException(401, "invalid API key")
    entities = db.scalars(
        select(m.Entity)
        .join(m.UserEntityGrant, m.UserEntityGrant.entity_id == m.Entity.id)
        .where(m.UserEntityGrant.user_id == user.id)
    ).all()
    return Principal(user, {e.id for e in entities}, {e.code: e for e in entities})


def get_principal(
    db: Session = Depends(get_db),
    x_api_key: str | None = Security(api_key_header),
) -> Principal:
    if not x_api_key:
        raise HTTPException(401, "X-API-Key header required")
    return load_principal(db, x_api_key)


def require_writer(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.can_write():
        raise HTTPException(403, "role does not permit write operations")
    return principal


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(403, "admin role required")
    return principal


def ensure_entity_access(db: Session, principal: Principal, entity_id: int | None) -> None:
    if entity_id is None:
        return
    if not principal.can_act_on(entity_id):
        raise HTTPException(403, "entity scope not granted to this principal")
