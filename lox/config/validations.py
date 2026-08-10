import os
from typing import Annotated, Literal

import msgspec

PROBLEMS: dict[str, str] = {}
"""Settings that are wrong but are not reasons to refuse to start.

A missing download directory, a hardlink target on the wrong volume, an image
host selected without its key: every one of these is something you fix on the
settings page — and you cannot reach the settings page if the process exits on
the way up. Refusing to boot over a value the UI exists to correct produces a
restart loop, which is strictly worse than starting up and saying what is wrong.

So these are collected here, reported by the API and shown as a banner, and the
operations that actually need the setting fail with a message naming it. Only
things that make the server unable to serve safely — a bad port, a weak auth
token — still stop startup.
"""


class BaseStruct(msgspec.Struct, forbid_unknown_fields=False):
    pass


def _note(key: str, message: str | None) -> None:
    """Record a problem against a settings key, or clear it if it is fixed."""
    if message:
        PROBLEMS[key] = message
    else:
        PROBLEMS.pop(key, None)


def problems() -> list[dict[str, str]]:
    """Everything currently wrong with the configuration, for the UI."""
    return [{"key": key, "message": message} for key, message in sorted(PROBLEMS.items())]


def ensure_dir(path: str, label: str) -> str | None:
    """Create a working directory lox owns, if it is not already there.

    Used for scratch and output directories only. Demanding the operator
    pre-create these defeats a zero-config container deploy, and getting them
    wrong is cheap — they hold artefacts lox regenerates.

    Args:
        path: Directory to create.
        label: Human-readable name of the setting, for the message.

    Returns:
        A description of what is wrong, or None if the directory is usable.
    """
    if os.path.isdir(path):
        return None
    if os.path.exists(path):
        return f"{label} is not a directory: {path}"
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        return f"{label} could not be created at {path}: {e}"
    return None


def require_dir(path: str, label: str) -> str | None:
    """Check a directory lox must not create.

    Args:
        path: Directory that has to already exist.
        label: Human-readable name of the setting, for the message.

    Returns:
        A description of what is wrong, or None if the directory is usable.
    """
    if os.path.isdir(path):
        return None
    if os.path.exists(path):
        return f"{label} is not a directory: {path}"
    return f"{label} does not exist: {path}. lox will not create it — check the volume mount, or set another path."


class Directory(BaseStruct):
    dottorrents_dir: str
    download_directory: str
    tmp_dir: str | None = None
    clean_tmp_dir: bool = False

    def __post_init__(self):
        self.check()

    def check(self) -> None:
        # Created on demand: these hold .torrent files and spectral scratch,
        # both of which lox produces itself.
        _note("directory.dottorrents_dir", ensure_dir(self.dottorrents_dir, "Torrent output directory"))
        _note("directory.tmp_dir", ensure_dir(self.tmp_dir, "Spectral scratch directory") if self.tmp_dir else None)

        # Never created. This is your music library; if it is missing, a volume
        # mount is wrong, and silently making an empty directory inside the
        # container would send downloads somewhere that vanishes on restart.
        _note("directory.download_directory", require_dir(self.download_directory, "Download directory"))


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
        self.check()

    def check(self) -> None:
        selected = {self.image_uploader, self.cover_uploader, self.specs_uploader}
        missing = [
            host
            for host, key in (
                ("ptpimg", self.ptpimg_key),
                ("ptscreens", self.ptscreens_key),
                ("oeimg", self.oeimg_key),
                ("imgbb", self.imgbb_key),
            )
            if host in selected and not key
        ]
        _note(
            "image.image_uploader",
            f"Image host {', '.join(missing)} is selected but has no API key set." if missing else None,
        )


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
        self.check()

    def check(self) -> None:
        # Deezer downloads land beside the main library; if the operator points
        # this somewhere new, create it rather than refusing to start.
        _note(
            "metadata.deezer.download_dir",
            ensure_dir(self.download_dir, "Deezer download directory") if self.download_dir else None,
        )


class Metadata(BaseStruct):
    discogs_token: str | None = None
    apple_music_token: str | None = None
    qobuz: QobuzSettings = msgspec.field(default_factory=QobuzSettings)
    tidal: TidalSettings = msgspec.field(default_factory=TidalSettings)
    deezer: DeezerSettings = msgspec.field(default_factory=DeezerSettings)


class GazelleTrackerSettings(BaseStruct):
    # Empty means "not configured yet" rather than an error, so the UI can add
    # a tracker after first boot.
    session: str = ""
    api_key: str | None = None
    # TODO: validate this
    dottorrents_dir: str | None = None


class Tracker(BaseStruct):
    # Each tracker section exists as soon as it is declared, even empty, so the
    # settings page has somewhere to write a session cookie into. Having no
    # tracker configured is allowed: you are expected to add one through the UI
    # on first run, and everything Deezer-side works without one.
    red: GazelleTrackerSettings = msgspec.field(default_factory=lambda: GazelleTrackerSettings(session=""))
    ops: GazelleTrackerSettings = msgspec.field(default_factory=lambda: GazelleTrackerSettings(session=""))
    dic: GazelleTrackerSettings = msgspec.field(default_factory=lambda: GazelleTrackerSettings(session=""))
    default_tracker: Literal["RED", "OPS", "DIC"] | None = None

    def configured(self) -> list[str]:
        """Tracker codes that have a session cookie or an API key."""
        return [
            code
            for code, settings in (("RED", self.red), ("OPS", self.ops), ("DIC", self.dic))
            if settings and (settings.session or settings.api_key)
        ]


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
    port: int = 5015
    static_root_url: str = "/static"
    # Host used in links shown to you, e.g. the spectral viewer. `host` is what
    # the socket binds to, which in a container is 0.0.0.0 - useless in a URL.
    # Set this to the address you actually reach the UI on.
    display_host: str = ""
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
    user_agent: str = "lox uploading tools"

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

    # Verbose logging, viewable in the UI under Settings. Credentials are
    # redacted before anything is written, so the log is safe to share.
    debug: bool = False

    # Do everything except the two irreversible steps: posting the torrent to
    # the tracker and handing it to the download client. Tagging, renaming,
    # spectrals, image uploads, hardlinking and .torrent creation all still run,
    # so you can inspect the result before committing to it.
    dry_run: bool = False

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

    # Where scan state is kept. Defaults to <download_directory>/.lox-checker.
    state_dir: str | None = None

    def __post_init__(self):
        self.check()

    def check(self) -> None:
        _note(
            "checker.state_dir",
            ensure_dir(self.state_dir, "Scan history directory") if self.state_dir else None,
        )


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
        self.check()

    def check(self) -> None:
        if self.enabled and not self.link_dir:
            _note("linking.link_dir", "Hardlinking is on but no seeding directory is set.")
        else:
            _note("linking.link_dir", ensure_dir(self.link_dir, "Seeding directory") if self.link_dir else None)


class Logging(BaseStruct):
    """On-disk rolling logs.

    Bounded twice over: no single file exceeds max_file_bytes, and the whole
    set is capped at max_total_bytes so a chatty run cannot fill the volume the
    config lives on.
    """

    # Defaults to <settings directory>/logs.
    directory: str | None = None
    max_file_bytes: Annotated[int, msgspec.Meta(ge=65536)] = 8 * 1024 * 1024
    max_total_bytes: Annotated[int, msgspec.Meta(ge=1048576)] = 1024 * 1024 * 1024

    def __post_init__(self):
        self.check()

    def check(self) -> None:
        _note(
            "logging.max_total_bytes",
            "Total log size is smaller than one log file." if self.max_total_bytes < self.max_file_bytes else None,
        )
        _note("logging.directory", ensure_dir(self.directory, "Log directory") if self.directory else None)

    @property
    def backup_count(self) -> int:
        """How many rotated files to keep so the total stays under the cap."""
        return max(1, (self.max_total_bytes // self.max_file_bytes) - 1)


class Notifications(BaseStruct):
    """Optional Discord webhook notifications for checker results."""

    enabled: bool = False
    discord_webhook: str | None = None
    # Post automatically when a scan finds something, rather than on request.
    notify_missing: bool = False
    notify_fillable: bool = False

    def __post_init__(self):
        self.check()

    def check(self) -> None:
        unset = self.enabled and not self.discord_webhook
        _note("notifications.discord_webhook", "Notifications are on but no Discord webhook is set." if unset else None)


class Cfg(BaseStruct):
    "This class defines the schema that msgspec uses to parse the config"

    directory: Directory
    metadata: Metadata = msgspec.field(default_factory=Metadata)
    image: ImageUploader = msgspec.field(default_factory=ImageUploader)
    tracker: Tracker = msgspec.field(default_factory=Tracker)
    seedbox: list[Seedbox] = msgspec.field(default_factory=list)
    upload: Upload = msgspec.field(default_factory=Upload)
    checker: Checker = msgspec.field(default_factory=Checker)
    logging: Logging = msgspec.field(default_factory=Logging)
    linking: Linking = msgspec.field(default_factory=Linking)
    notifications: Notifications = msgspec.field(default_factory=Notifications)


def validate(cfg: Cfg) -> list[dict[str, str]]:
    """Re-check everything that can be wrong without being fatal.

    The per-struct checks run once at parse time, which is before settings.toml
    is layered on and long before anyone edits a path in the UI. This runs them
    against the live config, so a directory you have just corrected on the
    settings page stops being reported without a restart — and one you have just
    broken starts being reported.

    Args:
        cfg: The live configuration.

    Returns:
        The remaining problems, newly recomputed.
    """
    PROBLEMS.clear()
    cfg.directory.check()
    cfg.metadata.deezer.check()
    cfg.image.check()
    cfg.checker.check()
    cfg.linking.check()
    cfg.logging.check()
    cfg.notifications.check()
    return problems()
