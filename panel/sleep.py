"""休眠判断隔离层：仅基于已采集的 IO 差分，不主动唤醒。"""

from .collector import DiskIdleTracker

__all__ = ["DiskIdleTracker"]
