"""Admin / setup endpoints: entities and users."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import Principal, get_principal, require_admin
from ..db import get_db
from ..schemas import EntityCreateRequest, UserCreateRequest
from ..util import key_hash

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.post("/entities", status_code=201)
def create_entity(
    body: EntityCreateRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
):
    """创建出口经营主体。主体是行级解析、授权与余额归属的边界。"""
    existing = db.scalar(select(m.Entity).where(m.Entity.code == body.code))
    if existing:
        raise HTTPException(409, f"entity {body.code} already exists")
    ent = m.Entity(code=body.code, name=body.name, country=body.country)
    db.add(ent)
    db.commit()
    return {"id": ent.id, "code": ent.code, "name": ent.name}


@router.get("/entities")
def list_entities(db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
    """列出当前 principal 有权访问的主体。admin 可见全部。"""
    """列出当前 principal 有权访问的主体。admin 可见全部。"""
    rows = db.scalars(select(m.Entity).order_by(m.Entity.code)).all()
    scoped = [r for r in rows if principal.can_act_on(r.id)]
    return [{"id": r.id, "code": r.code, "name": r.name, "country": r.country} for r in scoped]


@router.post("/users", status_code=201)
def create_user(
    body: UserCreateRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
):
    """创建账号并授予主体访问范围（role: admin/analyst/viewer）。API Key 仅以 SHA-256 存储。"""
    if db.scalar(select(m.User).where(m.User.name == body.name)):
        raise HTTPException(409, f"user {body.name} already exists")
    user = m.User(name=body.name, role=body.role, api_key_hash=key_hash(body.api_key))
    db.add(user)
    db.flush()
    for code in body.entity_codes:
        ent = db.scalar(select(m.Entity).where(m.Entity.code == code))
        if ent is None:
            raise HTTPException(422, f"entity code not found: {code}")
        db.add(m.UserEntityGrant(user_id=user.id, entity_id=ent.id))
    db.commit()
    return {"id": user.id, "name": user.name, "role": user.role, "entity_codes": body.entity_codes}


@router.get("/users")
def list_users(db: Session = Depends(get_db), principal: Principal = Depends(require_admin)):
    """列出账号及其角色与已授权主体（仅 admin）。"""
    """列出账号及其角色与已授权主体（仅 admin）。"""
    rows = db.scalars(select(m.User).order_by(m.User.name)).all()
    out = []
    for u in rows:
        codes = db.scalars(
            select(m.Entity.code)
            .join(m.UserEntityGrant, m.UserEntityGrant.entity_id == m.Entity.id)
            .where(m.UserEntityGrant.user_id == u.id)
        ).all()
        out.append({"id": u.id, "name": u.name, "role": u.role, "entity_codes": list(codes), "active": u.active})
    return out


@router.post("/users/{user_name}/grants", status_code=201)
def add_grant(
    user_name: str,
    entity_code: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
):
    """为已有账号追加一个主体授权（仅 admin）。"""
    user = db.scalar(select(m.User).where(m.User.name == user_name))
    ent = db.scalar(select(m.Entity).where(m.Entity.code == entity_code))
    if user is None or ent is None:
        raise HTTPException(404, "user or entity not found")
    exists = db.scalar(
        select(m.UserEntityGrant).where(
            m.UserEntityGrant.user_id == user.id, m.UserEntityGrant.entity_id == ent.id
        )
    )
    if exists is None:
        db.add(m.UserEntityGrant(user_id=user.id, entity_id=ent.id))
        db.commit()
    return {"user": user_name, "entity_code": entity_code, "granted": True}
