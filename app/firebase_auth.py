from typing import Any

import firebase_admin
from firebase_admin import auth
from firebase_admin import exceptions as firebase_exceptions

from app.config import settings


def _patch_firebase_error_compat() -> None:
    """Support legacy single-argument FirebaseError raises used by tests and older clients."""
    firebase_error_cls = getattr(firebase_exceptions, "FirebaseError", None)
    if firebase_error_cls is None or getattr(firebase_error_cls, "_trackin360_compat", False):
        return

    original_init = firebase_error_cls.__init__

    def compat_init(self: Exception, code_or_message: str, message: str | None = None, *args: object, **kwargs: object) -> None:
        if message is None:
            message = str(code_or_message)
            code_or_message = "internal"
        original_init(self, code_or_message, message, *args, **kwargs)

    firebase_error_cls.__init__ = compat_init
    firebase_error_cls._trackin360_compat = True


_patch_firebase_error_compat()


def verify_firebase_id_token(token: str) -> dict[str, Any]:
    try:
        firebase_app = firebase_admin.get_app()
    except ValueError:
        options = {"projectId": settings.firebase_project_id} if settings.firebase_project_id else None
        firebase_app = firebase_admin.initialize_app(options=options)
    return auth.verify_id_token(token, app=firebase_app, check_revoked=True)
