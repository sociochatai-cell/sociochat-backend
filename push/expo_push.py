"""
Send push notifications to a workspace's registered mobile devices via Expo's
Push service (https://exp.host/--/api/v2/push/send).

Flow: our server -> Expo Push API -> Google FCM (Android) / Apple APNs (iOS) -> phone.

Everything here is BEST-EFFORT and fully guarded: any failure is caught and
logged. It must NEVER raise into the caller (the webhook), so it can never break
message processing.
"""

import logging
import requests

from push.models import PushDevice

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def send_push_to_workspace(workspace_id, title: str, body: str, data: dict | None = None) -> int:
    """
    Send a notification to every device registered for this workspace.
    Returns the number of devices we attempted to notify (0 on any problem).
    Never raises.
    """
    try:
        if workspace_id is None:
            return 0

        devices = PushDevice.query.filter_by(workspace_id=workspace_id).all()
        tokens = [d.expo_token for d in devices if d.expo_token]
        if not tokens:
            return 0

        # Expo accepts a list of messages in one call.
        messages = [
            {
                "to": token,
                "title": title,
                "body": body,
                "sound": "default",
                "data": data or {},
                "channelId": "default",
            }
            for token in tokens
        ]

        resp = requests.post(
            EXPO_PUSH_URL,
            json=messages,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=8,
        )
        if resp.status_code >= 400:
            logger.warning("Expo push non-200: %s %s", resp.status_code, resp.text[:300])
        return len(tokens)
    except Exception as e:  # noqa: BLE001 - best effort, never break the caller
        logger.warning("send_push_to_workspace failed: %s", e)
        return 0
