"""Admin endpoints for the Gemini key pool and role to model routing.

Authentication: X-Service-Token header, constant-time compared against
SERVICE_TOKEN_SECRET (same pattern as /chat). Intended to be reached
only from the KPI backend proxy, never directly from browsers.

Audit: each mutating endpoint emits a logger.info line with the
principal and action. Response bodies NEVER include plaintext keys;
the /gemini-keys GET returns masked previews only.
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from config import settings
from services import key_store, role_router


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_service_token(
    x_service_token: Optional[str] = Header(None, alias="X-Service-Token"),
) -> None:
    expected = settings.SERVICE_TOKEN_SECRET or ""
    presented = x_service_token or ""
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid service token")


class AddKeyRequest(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    key: str = Field(min_length=30, max_length=500)
    created_by: Optional[str] = Field(default=None, max_length=150)


class TestKeyRequest(BaseModel):
    key: str = Field(min_length=30, max_length=500)


class RoleMappingItem(BaseModel):
    role_name: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    priority: int = 0


class RoleMapRequest(BaseModel):
    mappings: list[RoleMappingItem]
    updated_by: Optional[str] = Field(default=None, max_length=150)


@router.get("/gemini-keys", dependencies=[Depends(_require_service_token)])
async def list_gemini_keys():
    return {"keys": key_store.list_keys_masked()}


@router.post("/gemini-keys", dependencies=[Depends(_require_service_token)])
async def add_gemini_key(req: AddKeyRequest):
    try:
        key_id = key_store.add_key(req.label, req.key, req.created_by)
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=409, detail=f"label already exists: {req.label}") from e
    logger.info(
        "admin.add_gemini_key label=%s by=%s key_id=%s",
        req.label, req.created_by or "-", key_id,
    )
    return {"id": key_id, "label": req.label}


@router.delete("/gemini-keys/{key_id}", dependencies=[Depends(_require_service_token)])
async def delete_gemini_key(key_id: int):
    ok = key_store.delete_key(key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="key not found")
    logger.info("admin.delete_gemini_key key_id=%s", key_id)
    return {"deleted": key_id}


@router.post("/gemini-keys/test", dependencies=[Depends(_require_service_token)])
async def test_gemini_key(req: TestKeyRequest):
    """Make one minimal generateContent call against Gemini to verify
    the key works. Returns {valid, status_code, detail}."""
    url = f"{settings.GEMINI_BASE_URL.rstrip('/')}/models/{settings.GEMINI_MODEL}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": req.key}
    body = {
        "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
        "generationConfig": {"maxOutputTokens": 1},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, headers=headers, json=body)
        if resp.status_code == 200:
            return {"valid": True, "status_code": 200, "detail": ""}
        return {
            "valid": False,
            "status_code": resp.status_code,
            "detail": resp.text[:300],
        }
    except Exception as e:
        return {"valid": False, "status_code": 0, "detail": f"request failed: {e!s}"}


@router.get("/role-model-map", dependencies=[Depends(_require_service_token)])
async def get_role_model_map():
    return {"mappings": role_router.list_mappings()}


@router.put("/role-model-map", dependencies=[Depends(_require_service_token)])
async def put_role_model_map(req: RoleMapRequest):
    count = role_router.upsert_mappings(
        [m.model_dump() for m in req.mappings], req.updated_by
    )
    logger.info(
        "admin.put_role_model_map count=%s by=%s",
        count, req.updated_by or "-",
    )
    return {"count": count}


@router.delete("/role-model-map/{role_name}", dependencies=[Depends(_require_service_token)])
async def delete_role_mapping(role_name: str):
    ok = role_router.delete_mapping(role_name)
    if not ok:
        raise HTTPException(status_code=404, detail="mapping not found")
    logger.info("admin.delete_role_mapping role_name=%s", role_name)
    return {"deleted": role_name}
