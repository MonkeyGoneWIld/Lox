"""Tracker checking: does a Deezer release already exist, and does it fill a request?

Two flows share the same matching heuristics and the same rate-limited tracker
gateway:

* :mod:`lox.checker.missing` walks Deezer playlists and channel modules and
  reports albums that are not on RED/OPS yet.
* :mod:`lox.checker.requests_check` walks tracker requests and reports the
  ones that a Deezer release could fill.

Neither touches a tracker until explicitly told to, because tracker API budgets
are small and easy to burn.
"""

from lox.checker.deezer_requests import DeezerRequestChecker, RequestMatch
from lox.checker.gateway import TrackerBudgetExceeded, TrackerGateway, TrackerStatus
from lox.checker.missing import AlbumCheck, GroupHit, MissingScanner, ScanResult, TrackerVerdict
from lox.checker.store import CheckerStore
from lox.checker.watchlists import Watchlist, WatchlistManager

__all__ = [
    "AlbumCheck",
    "CheckerStore",
    "DeezerRequestChecker",
    "GroupHit",
    "MissingScanner",
    "RequestMatch",
    "ScanResult",
    "TrackerBudgetExceeded",
    "TrackerGateway",
    "TrackerStatus",
    "TrackerVerdict",
    "Watchlist",
    "WatchlistManager",
]
