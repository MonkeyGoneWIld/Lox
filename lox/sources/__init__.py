from lox import cfg
from lox.sources.bandcamp import BandcampBase
from lox.sources.beatport import BeatportBase
from lox.sources.deezer import DeezerBase
from lox.sources.discogs import DiscogsBase
from lox.sources.itunes import iTunesBase
from lox.sources.junodownload import JunodownloadBase
from lox.sources.musicbrainz import MusicBrainzBase
from lox.sources.qobuz import QobuzBase
from lox.sources.tidal import TidalBase

__all__ = [
    "cfg",
    "BandcampBase",
    "BeatportBase",
    "DeezerBase",
    "DiscogsBase",
    "iTunesBase",
    "JunodownloadBase",
    "MusicBrainzBase",
    "QobuzBase",
    "TidalBase",
    "SOURCE_ICONS",
]

SOURCE_ICONS = {
    "Bandcamp": "https://img.onlyimage.org/nzbFEn.png",
    "Beatport": "https://ptpimg.me/5hwjpv.png",
    "Deezer": "https://img.onlyimage.org/nzV3fO.png",
    "Discogs": "https://img.onlyimage.org/nzbmXc.png",
    "iTunes": "https://img.onlyimage.org/nzbDC0.png",
    "Junodownload": "https://ptpimg.me/u1rpx9.png",
    "MusicBrainz": "https://img.onlyimage.org/nzbK3p.png",
    "Qobuz": "https://img.onlyimage.org/nzbQ2V.png",
    "Tidal": "https://img.onlyimage.org/nzbG52.png",
}
