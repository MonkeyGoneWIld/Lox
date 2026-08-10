"""Deezer private (GW) API client, download engine and channel explorer.

The public api.deezer.com endpoints used elsewhere in lox are unauthenticated
and cannot see stream URLs, FLAC availability or channel modules. This package
talks to the internal gw-light endpoint with an ARL cookie instead.
"""

from lox.deezer.download import Downloader, DownloadError, DownloadJob, TrackDownload
from lox.deezer.explore import Explorer
from lox.deezer.gw import DeezerGW, DeezerGWError, TrackAvailability

__all__ = [
    "DeezerGW",
    "DeezerGWError",
    "DownloadError",
    "DownloadJob",
    "Downloader",
    "Explorer",
    "TrackAvailability",
    "TrackDownload",
]
