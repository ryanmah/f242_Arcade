"""What kind of thing is playing right now.

The player tags every item it plays with a content type at catalog-build time,
so this is a read of published state rather than a guess from file paths.
State comes from the sqlite bus rather than a polled file.
"""

from typing import Any, Dict, Optional

from fs42 import ipc


class ContentType:
    """Content type constants matching the values the player publishes."""
    FEATURE = "feature"
    COMMERCIAL = "commercial"
    BUMP = "bump"
    SIGN_OFF = "sign_off"
    OFF_AIR = "off_air"
    GUIDE = "guide"
    WEB = "web"
    MUSIC = "music"
    UNKNOWN = "unknown"


class ContentClassifier:
    """Reads the content type the player published with its status."""

    def _read_status(self) -> Optional[Dict[str, Any]]:
        try:
            return ipc.get_status() or None
        except Exception:
            return None

    def classify_from_status(self, status: Optional[Dict[str, Any]] = None) -> str:
        status_data = status if status is not None else self._read_status()
        if not status_data:
            return ContentType.UNKNOWN
        return status_data.get("content_type") or ContentType.UNKNOWN

    # Kept so existing callers and third-party scripts do not break.
    classify_from_socket = classify_from_status

    def classify_content(self, title=None, file_path=None, network_name=None) -> str:
        return self.classify_from_status()


def classify_current_content(status: Optional[Dict[str, Any]] = None) -> str:
    return ContentClassifier().classify_from_status(status)
