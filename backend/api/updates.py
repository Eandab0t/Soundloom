"""Update Center API.

Every mutating step is a distinct endpoint the UI must call explicitly:
check → download → install. There is deliberately no endpoint that does
more than one of those, so "check for updates" can never install.
"""
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from ..updater import manager
from ..updater.manager import UpdateError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["updates"])


class ComponentSelection(BaseModel):
    component_ids: list[str]


class ComponentId(BaseModel):
    component_id: str


@router.get("/api/updates/status")
async def updates_status():
    return manager.status()


@router.post("/api/updates/check")
async def updates_check():
    try:
        return await manager.check()
    except UpdateError as exc:
        return {"error": str(exc), "available": []}


@router.post("/api/updates/download")
async def updates_download(selection: ComponentSelection):
    try:
        return await manager.download(selection.component_ids)
    except UpdateError as exc:
        return {"error": str(exc), "results": {}}


@router.delete("/api/updates/download")
async def updates_download_cancel():
    return manager.cancel_download()


@router.post("/api/updates/install")
async def updates_install(selection: ComponentSelection):
    try:
        return await manager.install(selection.component_ids)
    except UpdateError as exc:
        return {"error": str(exc), "results": {}}


@router.get("/api/updates/history")
async def updates_history():
    return {"history": manager.history()}


@router.post("/api/updates/restore")
async def updates_restore(body: ComponentId):
    try:
        return await manager.restore(body.component_id)
    except UpdateError as exc:
        return {"error": str(exc)}
