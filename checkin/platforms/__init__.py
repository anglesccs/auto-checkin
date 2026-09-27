from .trae import TraePlatform
from .workbuddy import WorkBuddyPlatform

PLATFORMS = {
    "trae": TraePlatform,
    "workbuddy": WorkBuddyPlatform,
}

__all__ = ["PLATFORMS", "TraePlatform", "WorkBuddyPlatform"]
