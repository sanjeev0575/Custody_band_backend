import logging

import firebase_admin
from firebase_admin import messaging

logger = logging.getLogger(__name__)


def send_push_notifications(
    tokens: list[str],
    *,
    title: str,
    body: str,
    data: dict[str, str],
) -> None:
    if not tokens:
        return
    try:
        app = firebase_admin.get_app()
    except ValueError:
        app = firebase_admin.initialize_app()

    for token in tokens:
        try:
            messaging.send(
                messaging.Message(
                    notification=messaging.Notification(title=title, body=body),
                    data=data,
                    token=token,
                ),
                app=app,
            )
        except firebase_admin.exceptions.FirebaseError:
            logger.exception("Firebase Cloud Messaging could not deliver a notification")
