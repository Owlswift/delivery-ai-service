from fastapi import APIRouter

from app.api.v1.events import router as events_router
from app.api.v1.operations import router as operations_router
from app.api.v1.sessions import router as sessions_router

router = APIRouter(prefix="/v1")
router.include_router(sessions_router)
router.include_router(events_router)
router.include_router(operations_router)
