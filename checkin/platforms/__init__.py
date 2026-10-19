from .codearts import CodeArtsPlatform
from .qoder import QoderPlatform
from .trae import TraePlatform
from .workbuddy import WorkBuddyPlatform

PLATFORMS = {
    "trae": TraePlatform,
    "workbuddy": WorkBuddyPlatform,
    "qoder": QoderPlatform,
    "codearts": CodeArtsPlatform,
}

__all__ = ["PLATFORMS", "TraePlatform", "WorkBuddyPlatform", "QoderPlatform",
           "CodeArtsPlatform"]
