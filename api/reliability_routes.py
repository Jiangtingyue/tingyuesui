"""Read-only reliability state plus explicit cancellation endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from chat_reliability import (
    InvalidClientRequestId, chat_request_runtime, chat_request_store,
)
from relational_honesty import relational_honesty_guard
from schema_migrations import migration_status


router = APIRouter(tags=["reliability"])


@router.get("/api/chat/requests/{client_request_id}")
async def chat_request_status(client_request_id: str):
    try:
        item = chat_request_store.get(client_request_id)
    except InvalidClientRequestId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="没有找到这个聊天请求")
    return item


@router.post("/api/chat/requests/{client_request_id}/cancel")
async def cancel_chat_request(client_request_id: str):
    try:
        item = chat_request_store.request_cancel(client_request_id)
    except InvalidClientRequestId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="没有找到这个聊天请求")
    chat_request_runtime.cancel(client_request_id)
    return item


@router.get("/api/relational-honesty/status")
async def relational_honesty_status():
    return relational_honesty_guard.status()


@router.patch("/api/relational-honesty/settings")
async def relational_honesty_settings(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="设置必须是 JSON 对象")
    try:
        return relational_honesty_guard.update_settings(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/relational-honesty/audits")
async def relational_honesty_audits(limit: int = Query(30, ge=1, le=200)):
    return {"items": relational_honesty_guard.list_audits(limit)}


@router.get("/api/system/migrations")
async def database_migrations():
    return migration_status()
