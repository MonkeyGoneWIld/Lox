"""Deezer private (GW) API client, download engine and channel explorer.

The public api.deezer.com endpoints used elsewhere in salmon are unauthenticated
and cannot see stream URLs, FLAC availability or channel modules. This package
talks to the internal gw-light endpoint with an ARL cookie instead.
"""

from salmon.deezer.download import DownloadError, DownloadJob, Downloader, TrackDownload
from salmon.deezer.explore import Explorer
from salmon.deezer.gw import DeezerGWError, DeezerGW, TrackAvailability

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
