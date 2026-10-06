from .upload import router as upload_router
from .assets import router as assets_router
from .chat import router as chat_router
from .schedules import router as schedules_router

__all__ = ["upload_router", "assets_router", "chat_router", "schedules_router"]
