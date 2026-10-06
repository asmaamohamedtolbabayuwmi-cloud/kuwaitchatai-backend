from fastapi import UploadFile

from app.routers.schedules import import_schedule, router


def test_upload_file_annotation_is_resolved_for_fastapi():
    assert import_schedule.__annotations__["file"] is UploadFile


def test_schedule_import_route_is_registered():
    paths = {route.path for route in router.routes}
    assert "/schedules/import" in paths
