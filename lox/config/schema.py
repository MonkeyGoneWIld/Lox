"""Declarative description of the settings the UI can edit.

Every field the user should be able to change lives here rather than being
hand-written into the settings page. The UI renders itself from this list, and
the settings API validates writes against it, so adding a setting means adding
one entry.

What deliberately is *not* here: the handful of values needed to start the
server and find its data. Those have to exist before there is a UI to edit them
in, so they stay in config.toml — see BOOTSTRAP_KEYS.
"""

from typing import Any, Literal, NamedTuple

FieldKind = Literal["text", "secret", "int", "float", "bool", "choice", "path", "list"]

BOOTSTRAP_KEYS = (
    "upload.web_interface.host",
    "upload.web_interface.port",
    "upload.web_interface.auth_token",
    "directory.download_directory",
    "directory.dottorrents_dir",
)
"""Settings the server needs before it can serve the page that would otherwise
edit them. Supply them in config.toml or through the environment — see
BOOTSTRAP_ENV."""

BOOTSTRAP_ENV: dict[str, str] = {
    "LOX_HOST": "upload.web_interface.host",
    "LOX_PORT": "upload.web_interface.port",
    "LOX_AUTH_TOKEN": "upload.web_interface.auth_token",
    "LOX_DOWNLOAD_DIR": "directory.download_directory",
    "LOX_TORRENTS_DIR": "directory.dottorrents_dir",
    "LOX_TMP_DIR": "directory.tmp_dir",
    "LOX_STATE_DIR": "checker.state_dir",
    "LOX_DEBUG": "upload.debug",
    "LOX_LOG_DIR": "logging.directory",
    "LOX_DISPLAY_HOST": "upload.web_interface.display_host",
}
"""Environment variables that supply bootstrap settings, so a container can be
configured entirely from compose with no config file. The environment wins over
config.toml. Everything else is set in the UI."""


class Field(NamedTuple):
    """One editable setting."""

    key: str
    label: str
    kind: FieldKind
    section: str
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    placeholder: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the settings page."""
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "section": self.section,
            "help": self.help,
            "choices": list(self.choices),
            "min": self.minimum,
            "max": self.maximum,
            "placeholder": self.placeholder,
        }


class Section(NamedTuple):
    """A group of settings, optionally with a connection test."""

    id: str
    title: str
    blurb: str = ""
    test: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the settings page."""
        return {"id": self.id, "title": self.title, "blurb": self.blurb, "test": self.test}


SECTIONS: tuple[Section, ...] = (
    Section(
        "deezer",
        "Deezer",
        "The ARL is a full session credential — anyone holding it is logged into your account. "
        "Deezer → devtools → Application → Cookies → deezer.com → arl.",
        test="deezer",
    ),
    Section("red", "RED", "Session cookie from your browser. An API key with upload rights is preferred.", test="red"),
    Section("ops", "OPS", "Session cookie from your browser. An API key with upload rights is preferred.", test="ops"),
    Section("dic", "DIC", "DIC does not support API key authentication.", test="dic"),
    Section(
        "checker",
        "Tracker budget",
        "Nothing contacts a tracker until you press a check button. These numbers bound what one press can cost. "
        "The defaults are conservative guesses, not measured limits.",
    ),
    Section(
        "linking",
        "Seeding layout",
        "Hardlinked per-tracker folders, cross-seed style. The link directory must be on the same filesystem "
        "as your downloads.",
        test="linking",
    ),
    Section("torrent", "Torrent client", "Where finished uploads are injected for seeding.", test="qbittorrent"),
    Section("images", "Image hosting", "Where cover art and spectrals are uploaded.", test="images"),
    Section(
        "metadata",
        "Verification sources",
        "Used only to cross-check track counts when deciding whether a Deezer release can fill a request. "
        "More sources configured means fewer wrong editions get through.",
        test="discogs",
    ),
    Section("notifications", "Notifications", "Optional Discord webhook for scan results.", test="discord"),
    Section("upload", "Uploading", "Behaviour of the upload pipeline itself."),
    Section("formatting", "Naming", "How release folders and files are named."),
    Section("debug", "Debug", "Verbose logging and a diagnostics bundle. Credentials are never written."),
    Section("paths", "Paths", "Scratch and state directories. The main ones are set in config.toml.", test="paths"),
)


FIELDS: tuple[Field, ...] = (
    # --- Deezer -------------------------------------------------------
    Field("metadata.deezer.arl", "ARL cookie", "secret", "deezer", "Required for downloads, channels and FLAC checks."),
    Field("metadata.deezer.download_dir", "Download directory", "path", "deezer",
          "Where downloads land. Defaults to the main download directory."),
    Field("metadata.deezer.preferred_format", "Preferred quality", "choice", "deezer",
          choices=("FLAC", "MP3_320", "MP3_128")),
    Field("metadata.deezer.format_fallback", "Accept lower quality if unavailable", "bool", "deezer"),
    Field("metadata.deezer.concurrent_downloads", "Simultaneous track downloads", "int", "deezer",
          minimum=1, maximum=8),

    # --- Trackers -----------------------------------------------------
    Field("tracker.red.session", "Session cookie", "secret", "red"),
    Field("tracker.red.api_key", "API key", "secret", "red", "Needs upload privileges."),
    Field("tracker.red.dottorrents_dir", "Torrent output directory", "path", "red"),
    Field("tracker.ops.session", "Session cookie", "secret", "ops"),
    Field("tracker.ops.api_key", "API key", "secret", "ops", "Needs upload privileges."),
    Field("tracker.ops.dottorrents_dir", "Torrent output directory", "path", "ops"),
    Field("tracker.dic.session", "Session cookie", "secret", "dic"),
    Field("tracker.dic.dottorrents_dir", "Torrent output directory", "path", "dic"),
    Field("tracker.default_tracker", "Default tracker", "choice", "upload", choices=("RED", "OPS", "DIC")),

    # --- Checker ------------------------------------------------------
    Field("checker.tracker_budget", "Calls allowed per window", "int", "checker", minimum=1),
    Field("checker.tracker_budget_window", "Window length (seconds)", "int", "checker", minimum=10),
    Field("checker.tracker_call_delay", "Minimum gap between calls (seconds)", "float", "checker", minimum=0),
    Field("checker.tracker_switch_delay", "Pause when switching tracker (seconds)", "float", "checker", minimum=0),
    Field("checker.failure_threshold", "Failures before a tracker is benched", "int", "checker", minimum=1),
    Field("checker.cooldown_seconds", "Bench duration (seconds)", "int", "checker", minimum=0),
    Field("checker.min_tracks", "Ignore albums with fewer tracks than", "int", "checker", "0 disables.", minimum=0),
    Field("checker.min_date", "Ignore releases before", "text", "checker", "YYYY-MM-DD. Blank disables.",
          placeholder="2025-01-01"),
    Field("checker.max_date", "Ignore releases after", "text", "checker", "YYYY-MM-DD. Blank disables.",
          placeholder="2026-12-31"),
    Field("checker.min_confidence", "Minimum request match confidence", "float", "checker",
          "Artist and title must also clear their own thresholds.", minimum=0.0, maximum=1.0),
    Field("checker.state_dir", "Scan history directory", "path", "paths"),

    # --- Linking ------------------------------------------------------
    Field("linking.enabled", "Hardlink releases per tracker", "bool", "linking"),
    Field("linking.link_dir", "Seeding directory", "path", "linking", "Must share a filesystem with downloads."),
    Field("linking.method", "Method", "choice", "linking", choices=("hardlink", "symlink", "copy")),
    Field("linking.per_tracker_dirs", "Separate folder per tracker", "bool", "linking"),
    Field("linking.fallback_to_copy", "Fall back to a real copy if linking fails", "bool", "linking",
          "Leave off so a cross-filesystem mistake fails loudly instead of doubling disk usage."),

    # --- Images -------------------------------------------------------
    Field("image.image_uploader", "General images", "choice", "images",
          choices=("ptpimg", "ptscreens", "oeimg", "catbox", "imgbb", "imgbox")),
    Field("image.cover_uploader", "Cover art", "choice", "images",
          choices=("ptpimg", "ptscreens", "oeimg", "catbox", "imgbb", "imgbox")),
    Field("image.specs_uploader", "Spectrals", "choice", "images",
          choices=("ptpimg", "ptscreens", "oeimg", "catbox", "imgbb", "imgbox")),
    Field("image.ptpimg_key", "ptpimg key", "secret", "images"),
    Field("image.ptscreens_key", "ptscreens key", "secret", "images"),
    Field("image.oeimg_key", "oeimg key", "secret", "images"),
    Field("image.imgbb_key", "imgbb key", "secret", "images"),
    Field("image.auto_compress_cover", "Compress covers automatically", "bool", "images"),
    Field("image.remove_auto_downloaded_cover_image", "Delete covers lox downloaded", "bool", "images"),

    # --- Verification sources ----------------------------------------
    Field("metadata.discogs_token", "Discogs token", "secret", "metadata",
          "discogs.com → Settings → Developers."),
    Field("metadata.apple_music_token", "Apple Music developer token", "secret", "metadata"),
    Field("metadata.qobuz.app_id", "Qobuz app ID", "text", "metadata"),
    Field("metadata.qobuz.user_auth_token", "Qobuz auth token", "secret", "metadata"),
    Field("metadata.tidal.token", "Tidal token", "secret", "metadata"),

    # --- Notifications ------------------------------------------------
    Field("notifications.enabled", "Send notifications", "bool", "notifications"),
    Field("notifications.discord_webhook", "Discord webhook URL", "secret", "notifications"),
    Field("notifications.notify_missing", "Post when a scan finds a missing album", "bool", "notifications",
          "A scan finding fifty albums fires fifty webhooks."),
    Field("notifications.notify_fillable", "Post when a request can be filled", "bool", "notifications"),

    # --- Upload -------------------------------------------------------
    Field("upload.dry_run", "Dry run", "bool", "upload",
          "Do everything except post to the tracker and add to the download client."),
    Field("upload.yes_all", "Auto-answer prompts", "bool", "upload",
          "The lossy-master question always asks regardless."),
    Field("upload.multi_tracker_upload", "Allow uploading to several trackers in one run", "bool", "upload"),
    Field("upload.upload_to_seedbox", "Inject into the torrent client", "bool", "upload"),
    Field("upload.simultaneous_threads", "Worker threads", "int", "upload", minimum=1, maximum=16),
    Field("upload.default_editor", "Text editor", "text", "upload", placeholder="nano"),
    Field("upload.debug", "Debug mode", "bool", "debug",
          "Verbose logging, shown below. Credentials are redacted before anything is written."),
    Field("upload.debug_tracker_connection", "Also log tracker requests and responses", "bool", "debug", "Noisy."),
    Field("upload.web_interface.display_host", "Address you reach the UI on", "text", "upload",
          "Used in links like the spectral viewer. Leave blank unless the bind address is 0.0.0.0.",
          placeholder="192.168.1.25"),
    Field("logging.directory", "Log directory", "path", "debug",
          "Defaults to a logs folder beside settings.toml."),
    Field("logging.max_file_bytes", "Maximum size per log file (bytes)", "int", "debug",
          "8388608 is 8 MB.", minimum=65536),
    Field("logging.max_total_bytes", "Maximum total log size (bytes)", "int", "debug",
          "1073741824 is 1 GB. Older files are deleted to stay under it.", minimum=1048576),
    Field("upload.update_notification", "Check for lox updates on startup", "bool", "upload"),
    Field("upload.compression.flac_compression_level", "FLAC compression level", "int", "upload",
          minimum=0, maximum=8),
    Field("upload.compression.compress_spectrals", "Compress spectrals before upload", "bool", "upload"),
    Field("upload.requests.check_requests", "Check for fillable requests during upload", "bool", "upload"),
    Field("upload.requests.last_minute_dupe_check", "Re-check for duplicates before uploading", "bool", "upload"),

    # --- Formatting ---------------------------------------------------
    Field("upload.formatting.folder_template", "Folder template", "text", "formatting",
          placeholder="{artists} - {title} ({year}) [{source} {format}]"),
    Field("upload.formatting.file_template", "File template", "text", "formatting",
          placeholder="{tracknumber}. {artist} - {title}"),
    Field("upload.formatting.one_album_artist_file_template", "File template, single artist", "text", "formatting"),
    Field("upload.formatting.no_artist_in_filename_if_only_one_album_artist",
          "Drop the artist from filenames when there is only one", "bool", "formatting"),
    Field("upload.formatting.various_artist_threshold", "Artists before a release is Various", "int", "formatting",
          minimum=2),
    Field("upload.formatting.various_artist_word", "Various Artists label", "text", "formatting"),
    Field("upload.formatting.strip_useless_versions", "Strip redundant version suffixes", "bool", "formatting"),
    Field("upload.formatting.lowercase_cover", "Lowercase the cover filename", "bool", "formatting"),
    Field("upload.description.icons_in_descriptions", "Icons in descriptions", "bool", "formatting"),
    Field("upload.description.include_tracklist_in_t_desc", "Tracklist in torrent description", "bool", "formatting"),
    Field("upload.description.bitrates_in_t_desc", "Bitrates in torrent description", "bool", "formatting"),

    # --- Paths --------------------------------------------------------
    Field("directory.tmp_dir", "Spectral scratch directory", "path", "paths"),
    Field("directory.clean_tmp_dir", "Wipe scratch directory at startup", "bool", "paths"),
)

FIELDS_BY_KEY: dict[str, Field] = {f.key: f for f in FIELDS}


def sections_with_fields() -> list[dict[str, Any]]:
    """Return sections, each carrying its own fields, in display order."""
    out = []
    for section in SECTIONS:
        fields = [f.as_dict() for f in FIELDS if f.section == section.id]
        if fields:
            out.append({**section.as_dict(), "fields": fields})
    return out
