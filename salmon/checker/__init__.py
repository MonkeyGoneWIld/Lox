"""Tracker checking: does a Deezer release already exist, and does it fill a request?

Two flows share the same matching heuristics and the same rate-limited tracker
gateway:

* :mod:`salmon.checker.missing` walks Deezer playlists and channel modules and
  reports albums that are not on RED/OPS yet.
* :mod:`salmon.checker.requests_check` walks tracker requests and reports the
  ones that a Deezer release could fill.

Neither touches a tracker until explicitly told to, because tracker API budgets
are small and easy to burn.
"""

from salmon.checker.gateway import TrackerBudgetExceeded, TrackerGateway, TrackerStatus
from salmon.checker.missing import AlbumCheck, GroupHit, MissingScanner, ScanResult, TrackerVerdict
from salmon.checker.requests_check import RequestChecker, RequestMatch
from salmon.checker.store import CheckerStore
from salmon.checker.watchlists import Watchlist, WatchlistManager

__all__ = [
    "AlbumCheck",
    "CheckerStore",
    "GroupHit",
    "MissingScanner",
    "RequestChecker",
    "RequestMatch",
    "ScanResult",
    "TrackerBudgetExceeded",
    "TrackerGateway",
    "TrackerStatus",
    "TrackerVerdict",
    "Watchlist",
    "WatchlistManager",
]
