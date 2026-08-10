import os
from typing import Annotated, Literal

import msgspec


class BaseStruct(msgspec.Struct, forbid_unknown_fields=False):
    pass


class Directory(BaseStruct):
    dottorrents_dir: str
    download_directory: str
    tmp_dir: str | None = None
    clean_tmp_dir: bool = False

    def __post_init__(self):
        if not os.path.isdir(self.dottorrents_dir):
            raise ValueError("dottorrents_dir is not a valid directory")
        if not os.path.isdir(self.download_directory):
            raise ValueError("download_directory is not a valid directory")
        if self.tmp_dir and not os.path.isdir(self.tmp_dir):
            raise ValueError("tmp_dir is not a valid directory")


ImgUploaderLiteral = Literal["ptpimg", "ptscreens", "oeimg", "catbox", "imgbb", "imgbox"]


class ImageUploader(BaseStruct):
    image_uploader: ImgUploaderLiteral = "catbox"
    cover_uploader: ImgUploaderLiteral = "catbox"
    specs_uploader: ImgUploaderLiteral = "catbox"
    ptpimg_key: str | None = None
    ptscreens_key: str | None = None
    oeimg_key: str | None = None
    imgbb_key: str | None = None
    remove_auto_downloaded_cover_image: bool = False
    auto_compress_cover: bool = False

    def __post_init__(self):
        uploader_selections = set({self.image_uploader, self.cover_uploader, self.specs_uploader})
        if ("ptpimg" in uploader_selections) and self.ptpimg_key is None:
            raise ValueError("ptpimg key not specified")
        if "ptscreens" in uploader_selections and self.ptscreens_key is None:
            raise ValueError("PTScreens key not specified")
        if "oeimg" in uploader_selections and self.oeimg_key is None:
            raise ValueError("OEImage key not specified")
        if "imgbb" in uploader_selections and self.imgbb_key is None:
            raise ValueError("imgbb key not specified")


class TidalSettings(BaseStruct):
    token: str | None = None
    search_regions: list[str] = msgspec.field(default_factory=lambda: ["de", "nz", "us", "gb"])
    fetch_regions: list[str] = msgspec.field(default_factory=lambda: ["de", "nz", "us", "gb"])


# TODO: Add validations here
class QobuzSettings(BaseStruct):
    app_id: str | None = None
    user_auth_token: str | None = None
    no_genres_from_qobuz: bool = False


class DeezerSettings(BaseStruct):
    arl: str | None = None
    # Where finished downloads land. Falls back to directory.download_directory.
    download_dir: str | None = None
    preferred_format: Literal["FLAC", "MP3_320", "MP3_128"] = "FLAC"
    # Accept a lower quality when the preferred one is not available to the account.
    format_fallback: bool = True
    concurrent_downloads: Annotated[int, msgspec.Meta(ge=1, le=8)] = 2

    def __post_init__(self):
        if self.download_dir and not os.path.isdir(self.download_dir):
            raise ValueError("metadata.deezer.download_dir is not a valid directory")


class Metadata(BaseStruct):
    discogs_token: str | None = None
    apple_music_token: str | None = None
    qobuz: QobuzSettings = msgspec.field(default_factory=QobuzSettings)
    tidal: TidalSettings = msgspec.field(default_factory=TidalSettings)
    deezer: DeezerSettings = msgspec.field(default_factory=DeezerSettings)


class GazelleTrackerSettings(BaseStruct):
    session: str
    api_key: str | None = None
    # TODO: validate this
    dottorrents_dir: str | None = None


class Tracker(BaseStruct):
    red: GazelleTrackerSettings | None = None
    ops: GazelleTrackerSettings | None = None
    dic: GazelleTrackerSettings | None = None
    default_tracker: Literal["RED", "OPS", "DIC"] | None = None

    def __post_init__(self):
        if (self.red is None) and (self.ops is None) and (self.dic is None):
            raise ValueError("You need a tracker session cookie in your config!")

        if self.ops is None and self.default_tracker == "OPS":
            raise ValueError("Default tracker is invalid!")
        if self.red is None and self.default_tracker == "RED":
            raise ValueError("Default tracker is invalid!")
        if self.dic is None and self.default_tracker == "DIC":
            raise ValueError("Default tracker is invalid!")


class Seedbox(BaseStruct):
    name: str = ""
    enabled: bool = False
    url: str = ""  # Name of remote in rclone
    type: Literal["local", "rclone"] = "local"
    # Directory when adding torrent to download client. Supports {tracker},
    # which expands to the tracker code the upload went to (RED, OPS, DIC) -
    # match this to linking.link_dir when per_tracker_dirs is on.
    directory: str = ""
    flac_only: bool = False  # if true, only upload FLAC files
    extra_args: list[str] = msgspec.field(default_factory=list)  # pass these arguments to rclone
    torrent_client: str = ""
    # Category (qBittorrent) or label (Transmission/Deluge/ruTorrent) to apply.
    # Also supports {tracker}.
    label: str = ""
    add_paused: bool = False  # If true, add torrents to client in paused state
    # Restrict this entry to one tracker. Unset means it handles every tracker.
    tracker: Literal["RED", "OPS", "DIC"] | None = None

    def __post_init__(self):
        if self.type not in ("local", "rclone"):
            raise ValueError("Invalid seedbox type specified")


class UploadSearch(BaseStruct):
    limit: int = 3
    # TODO: are these reasonable defaults?
    excluded_labels: list[str] = msgspec.field(default_factory=lambda: ["edm comps"])
    blacklisted_genres: list[str] = msgspec.field(default_factory=lambda: ["Soundtrack", "Asian Music"])


class UploadFormatting(BaseStruct):
    folder_template: str = "{artists} - {title} ({year}) [{source} {format}]"
    file_template: str = "{tracknumber}. {artist} - {title}"

    # formatting options
    no_artist_in_filename_if_only_one_album_artist: bool = True
    one_album_artist_file_template: str = "{tracknumber}. {title}"
    lowercase_cover: bool = True
    various_artist_threshold: int = 4
    blacklisted_substitution: str = "_"
    guests_in_track_title: bool = False
    various_artist_word: str = "Various"
    strip_useless_versions: bool = True
    add_edition_title_to_album_tag: bool = True


class UploadDescription(BaseStruct):
    bitrates_in_t_desc: bool = False
    include_tracklist_in_t_desc: bool = False
    copy_uploaded_url_to_clipboard: bool = False
    # TODO: should this be in description?
    review_as_comment_tag: bool = True
    icons_in_descriptions: bool = True
    # TODO: should this be in description?
    fullwidth_replacements: bool = False
    # TODO: should this be in description?
    empty_track_comment_tag: bool = True


class UploadWebInterface(BaseStruct):
    host: str = "127.0.0.1"
    port: int = 55015
    static_root_url: str = "/static"
    # Shared secret required by every API call. Unset means no authentication,
    # which is only safe while host stays on loopback: the API can spend tracker
    # budget, read your Deezer session and start uploads.
    auth_token: str | None = None

    def __post_init__(self):
        if self.port < 1 or self.port > 65535:
            raise ValueError("Port number is invalid")
        if self.auth_token is not None and len(self.auth_token) < 16:
            raise ValueError("upload.web_interface.auth_token must be at least 16 characters")


class UploadRequests(BaseStruct):
    always_ask_for_request_fill: bool = False
    check_recent_uploads: bool = True
    check_requests: bool = True
    last_minute_dupe_check: bool = False


class UploadCompression(BaseStruct):
    flac_compression_level: Annotated[int, msgspec.Meta(ge=0, le=8)] = 8
    compress_spectrals: bool = True
    # TODO: this probably should be in description
    lma_comment_in_t_desc: bool = False
    use_upc_as_catno: bool = True


class Upload(BaseStruct):
    simultaneous_threads: int = 3
    user_agent: str = "salmon uploading tools"

    # Default text editor for click.edit operations
    # Can be "nano", "vim", "emacs", or any command available in PATH
    default_editor: str | None = None

    native_spectrals_viewer: bool = False
    feh_fullscreen: bool = True
    prompt_puddletag: bool = False
    # must be within 0-1
    log_dupe_tolerance: Annotated[float, msgspec.Meta(ge=0.0, le=1.0)] = 0.5
    windows_use_recycle_bin: bool = True

    multi_tracker_upload: bool = True
    # TODO: should this be in tracker?
    debug_tracker_connection: bool = False

    update_notification: bool = True
    update_notification_verbose: bool = True

    yes_all: bool = False

    upload_to_seedbox: bool = True

    # TODO: take these out of the upload struct!
    search: UploadSearch = msgspec.field(default_factory=UploadSearch)
    formatting: UploadFormatting = msgspec.field(default_factory=UploadFormatting)
    description: UploadDescription = msgspec.field(default_factory=UploadDescription)
    web_interface: UploadWebInterface = msgspec.field(default_factory=UploadWebInterface)
    requests: UploadRequests = msgspec.field(default_factory=UploadRequests)
    compression: UploadCompression = msgspec.field(default_factory=UploadCompression)


class Checker(BaseStruct):
    """Budgets and filters for the tracker checkers.

    Tracker APIs punish bursts, so nothing here is a background job: the UI only
    spends budget when you press a check button. These numbers bound how much a
    single press can cost.
    """

    # Hard ceiling on tracker calls in any rolling window. A check refuses to
    # start rather than overdraw this.
    tracker_budget: Annotated[int, msgspec.Meta(ge=1)] = 120
    tracker_budget_window: Annotated[int, msgspec.Meta(ge=10)] = 600
    # Minimum gap between two calls to the same tracker.
    tracker_call_delay: Annotated[float, msgspec.Meta(ge=0.0)] = 2.0
    # Extra pause when moving from one tracker to another.
    tracker_switch_delay: Annotated[float, msgspec.Meta(ge=0.0)] = 5.0
    # Consecutive failures before a tracker is benched.
    failure_threshold: Annotated[int, msgspec.Meta(ge=1)] = 3
    cooldown_seconds: Annotated[int, msgspec.Meta(ge=0)] = 300

    # Album filters, applied before any tracker is contacted.
    min_tracks: Annotated[int, msgspec.Meta(ge=0)] = 0
    min_date: str | None = None
    max_date: str | None = None

    # Minimum confidence for a Deezer release to be offered as a request fill.
    min_confidence: Annotated[float, msgspec.Meta(ge=0.0, le=1.0)] = 0.70

    # Where scan state is kept. Defaults to <download_directory>/.salmon-checker.
    state_dir: str | None = None


class Linking(BaseStruct):
    """Hardlinked per-tracker release folders, cross-seed style.

    Uploading one release to two trackers needs two torrents pointed at two
    paths. Hardlinking means those paths cost no extra disk.
    """

    enabled: bool = False
    # Root of the torrent client's upload/seeding area.
    link_dir: str | None = None
    method: Literal["hardlink", "symlink", "copy"] = "hardlink"
    # <link_dir>/<TRACKER>/<release> when true, <link_dir>/<release> when false.
    per_tracker_dirs: bool = True
    # Hardlinks cannot cross filesystems. Copy instead of failing when that happens.
    fallback_to_copy: bool = False

    def __post_init__(self):
        if self.enabled and not self.link_dir:
            raise ValueError("linking.enabled is true but linking.link_dir is not set")
        if self.link_dir and not os.path.isdir(self.link_dir):
            raise ValueError("linking.link_dir is not a valid directory")


class Notifications(BaseStruct):
    """Optional Discord webhook notifications for checker results."""

    enabled: bool = False
    discord_webhook: str | None = None
    # Post automatically when a scan finds something, rather than on request.
    notify_missing: bool = False
    notify_fillable: bool = False

    def __post_init__(self):
        if self.enabled and not self.discord_webhook:
            raise ValueError("notifications.enabled is true but no discord_webhook is set")


class Cfg(BaseStruct):
    "This class defines the schema that msgspec uses to parse the config"

    directory: Directory
    metadata: Metadata = msgspec.field(default_factory=Metadata)
    image: ImageUploader = msgspec.field(default_factory=ImageUploader)
    tracker: Tracker = msgspec.field(default_factory=Tracker)
    seedbox: list[Seedbox] = msgspec.field(default_factory=list)
    upload: Upload = msgspec.field(default_factory=Upload)
    checker: Checker = msgspec.field(default_factory=Checker)
    linking: Linking = msgspec.field(default_factory=Linking)
    notifications: Notifications = msgspec.field(default_factory=Notifications)
