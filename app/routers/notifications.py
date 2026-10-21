"""HTTP API for typed push notifications and announcement history."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from firebase_admin import auth

from ..auth import verify_admin
from ..limiter import limiter
from ..services.notification_service import (
    AnnouncementDeliveryError,
    AnnouncementIdempotencyConflict,
    NotificationRepository,
    NotificationService,
    AnnouncementStorageError,
    ConfirmedPreSendNotificationError,
    viewer_identity,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])
_optional_bearer = HTTPBearer(auto_error=False)


class AnnouncementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=1000)


class AppUpdateRequest(AnnouncementRequest):
    target_version: str = Field(min_length=3, max_length=32)
    version: str = Field(min_length=3, max_length=32)
    force: bool = False


class AnnouncementViewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installationId: str = Field(
        min_length=20,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


async def optional_uid(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_optional_bearer)],
) -> str | None:
    if credentials is None:
        return None
    try:
        return auth.verify_id_token(credentials.credentials)["uid"]
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired Firebase ID token.") from exc


@router.post("/announcements", status_code=status.HTTP_201_CREATED)
async def create_announcement(
    request: AnnouncementRequest,
    response: Response,
    admin_uid: str = Depends(verify_admin),
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
):
    try:
        result = await NotificationService().create_announcement(
            request.title,
            request.body,
            admin_uid,
            idempotency_key=idempotency_key,
        )
        if result["deliveryStatus"] == "pending":
            response.status_code = status.HTTP_202_ACCEPTED
        elif not result.get("created", True):
            response.status_code = status.HTTP_200_OK
        return result
    except AnnouncementIdempotencyConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "idempotency_conflict", "message": str(exc)},
        ) from exc
    except AnnouncementStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "announcement_creation_unknown",
                "announcementCreated": None,
                "pushDispatched": False,
                "idempotencyKey": exc.idempotency_key,
                "deliveryStatus": None,
                "message": str(exc),
            },
        ) from exc
    except AnnouncementDeliveryError as exc:
        code = (
            "push_not_dispatched"
            if exc.delivery_status == "confirmed_pre_dispatch_failure"
            else "push_delivery_unknown"
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": code,
                "announcementId": exc.announcement_id,
                "idempotencyKey": exc.idempotency_key,
                "deliveryStatus": exc.delivery_status,
                "message": str(exc),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/app-update")
async def send_app_update(
    request: AppUpdateRequest,
    _admin_uid: str = Depends(verify_admin),
):
    try:
        return await NotificationService().send_app_update(
            title=request.title,
            body=request.body,
            target_version=request.target_version,
            new_version=request.version,
            force=request.force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConfirmedPreSendNotificationError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "push_not_dispatched",
                "deliveryStatus": "confirmed_pre_dispatch_failure",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "push_delivery_unknown",
                "deliveryStatus": "delivery_unknown",
            },
        ) from exc


@router.get("/announcements/latest")
async def latest_announcements():
    return {"announcements": await NotificationRepository().latest_announcements(3)}


@router.get("/announcements/stats")
async def announcement_stats(_admin_uid: str = Depends(verify_admin)):
    return {
        "announcements": await NotificationRepository().announcement_stats(),
        "viewCountsAreApproximate": True,
    }


@router.post("/announcements/{announcement_id}/views")
@limiter.limit("20/minute")
async def record_announcement_view(
    announcement_id: str,
    payload: AnnouncementViewRequest,
    request: Request,
    uid: str | None = Depends(optional_uid),
):
    if len(announcement_id) > 128 or "/" in announcement_id:
        raise HTTPException(status_code=422, detail="Invalid announcement identifier.")
    try:
        identity = viewer_identity(installation_id=payload.installationId, uid=uid)
        counted = await NotificationRepository().record_view(
            announcement_id, identity, authenticated=uid is not None
        )
        return {"counted": counted}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
