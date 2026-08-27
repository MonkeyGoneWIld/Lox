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

FieldKind = Literal["text", "secret", "int", "float", "bool", "choice", "path", "list", "bytes"]
"""``bytes`` is stored as a plain integer count; the UI edits it as a number and
a unit, because nobody should have to type 1073741824 and count the digits."""

BOOTSTRAP_KEYS = (
    "upload.web_interface.host",
    "upload.web_interface.port",
    "upload.web_interface.auth_token",
)
"""Settings the server needs before it can serve the page that would otherwise
edit them: what to bind, and who is allowed in. Supply them in config.toml or
through the environment — see BOOTSTRAP_ENV.

The directories are deliberately not here. They are bootstrapped from the
environment too, but the server does not need them to serve a page, and a wrong
path has to be fixable from the UI rather than only by editing compose."""

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
    labels: tuple[str, ...] = ()
    """What each choice is called on screen, in the same order as ``choices``.
    Stored values want to be short and stable ("only_missing_there"); the thing
    a person reads off a dropdown does not. Empty means the value is already
    the label, which is true of a list of image hosts and false of a rule."""
    minimum: float | None = None
    maximum: float | None = None
    placeholder: str = ""
    test: str = ""
    """A check that belongs to this one field. Sections holding several
    independent credentials -- four image-host keys, five metadata sources --
    cannot be tested by a single button at the top: it can only report on one
    of them, which is what "Test connection" beside five tokens was doing."""

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the settings page."""
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "section": self.section,
            "help": self.help,
            "choices": list(self.choices),
            "labels": list(self.labels),
            "min": self.minimum,
            "max": self.maximum,
            "placeholder": self.placeholder,
            "test": self.test,
        }


class Section(NamedTuple):
    """A group of settings, optionally with a connection test."""

    id: str
    title: str
    blurb: str = ""
    test: str = ""
    category: str = "General"
    """Which group of the page this belongs under. Sixteen sections in one
    column is a scroll, and the answer to "where do I set the seeding
    directory" should not be "somewhere below the fold"."""

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the settings page."""
        return {
            "id": self.id,
            "title": self.title,
            "blurb": self.blurb,
            "test": self.test,
            "category": self.category,
        }


SECTIONS: tuple[Section, ...] = (
    Section(
        "deezer",
        "Deezer",
        "The ARL is a full session credential — anyone holding it is logged into your account. "
        "Deezer → devtools → Application → Cookies → deezer.com → arl.",
        test="deezer",
        category="Accounts",
    ),
    Section("red", "RED", "Session cookie from your browser. An API key with upload rights is preferred.",
            test="red", category="Accounts"),
    Section("ops", "OPS", "Session cookie from your browser. An API key with upload rights is preferred.",
            test="ops", category="Accounts"),
    Section("dic", "DIC", "DIC does not support API key authentication.", test="dic", category="Accounts"),
    Section(
        "images",
        "Image hosting",
        "Where cover art and spectrals are uploaded. Each key has its own test, because one button at the "
        "top of four independent credentials can only ever report on one of them.",
        category="Accounts",
    ),
    Section(
        "metadata",
        "Verification sources",
        "Used only to cross-check track counts when deciding whether a Deezer release can fill a request. "
        "More sources configured means fewer wrong editions get through. Each has its own test.",
        category="Accounts",
    ),
    Section("notifications", "Notifications", "Optional Discord webhook for scan results.",
            test="discord", category="Accounts"),
    Section(
        "checker",
        "Tracker budget",
        "Nothing contacts a tracker until you press a check button. These numbers bound what one press can cost. "
        "The defaults are conservative guesses, not measured limits.",
        category="Accounts",
    ),
    Section(
        "scanning",
        "What gets checked",
        "Applied to a release before any tracker is contacted, so anything ruled out here costs nothing. "
        "These decide which releases are worth looking at, not how often a tracker may be asked.",
        category="Accounts",
    ),
    Section(
        "queue",
        "What reaches the queue",
        "Everything a check found is kept. This decides which of it is worth acting on, and it is applied when "
        "the queue is drawn -- so widening it brings rows back without spending tracker budget again.",
        category="Accounts",
    ),
    Section("upload", "Uploading", "How the pipeline behaves while it works through a release.",
            category="Uploading"),
    Section("requests", "Requests and duplicates", "What the pipeline checks against the tracker while uploading.",
            category="Uploading"),
    Section("formatting", "Naming", "How release folders and files are named.", category="Uploading"),
    Section("description", "Descriptions", "What goes in the torrent and group descriptions.",
            category="Uploading"),
    Section(
        "linking",
        "Seeding layout",
        "Hardlinked per-tracker folders, cross-seed style. The link directory must be on the same filesystem "
        "as your downloads.",
        test="linking",
        category="Files",
    ),
    Section(
        "torrent",
        "Torrent client",
        "Where a finished upload is handed over to start seeding. Pick the program, say where it is and "
        "which account to use, and Test connection will tell you whether it answered — before you save it.",
        test="qbittorrent",
        category="Files",
    ),
    Section(
        "paths",
        "Paths",
        "Where lox reads releases from and writes its own files to. Bootstrapped from the environment, "
        "but anything set here wins — so a wrong mount is fixable without editing compose.",
        test="paths",
        category="Files",
    ),
    Section("debug", "Debug", "Verbose logging and a diagnostics bundle. Credentials are never written.",
            category="Maintenance"),
)


CATEGORIES: tuple[str, ...] = ("Accounts", "Uploading", "Files", "Maintenance")
"""Display order for the section groups."""


#: The trackers a queue rule can name. Fixed, because these are the trackers
#: the app has clients for; the page only offers the ones you have configured.
QUEUE_TRACKERS: tuple[str, ...] = ("RED", "OPS", "DIC")

#: Every queue rule as (stored value, what it says on screen). Each label is a
#: whole sentence about a situation, because that is what someone is choosing
#: between -- the version this replaced made them assemble one out of a
#: three-way dropdown per tracker and an all/any to combine them.
#:
#: Lives here rather than beside the predicate because the config layer imports
#: nothing from lox, so both the settings page and lox.checker.queue_rules can
#: read it without a cycle.
QUEUE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("any", "Missing from at least one tracker"),
    ("all", "Missing from every tracker"),
    *((code, f"Missing from {code}") for code in QUEUE_TRACKERS),
    *((f"{code}_only", f"Missing from {code}, and already on the others") for code in QUEUE_TRACKERS),
)
QUEUE_CHOICES = tuple(value for value, _ in QUEUE_OPTIONS)
QUEUE_LABELS = tuple(label for _, label in QUEUE_OPTIONS)


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
    Field("tracker.red.dottorrents_dir", "Torrent output directory", "path", "red",
          "Only if RED's .torrent files should go somewhere of their own. Blank uses the one under Paths."),
    Field("tracker.ops.session", "Session cookie", "secret", "ops"),
    Field("tracker.ops.api_key", "API key", "secret", "ops", "Needs upload privileges."),
    Field("tracker.ops.dottorrents_dir", "Torrent output directory", "path", "ops",
          "Only if OPS's .torrent files should go somewhere of their own. Blank uses the one under Paths."),
    Field("tracker.dic.session", "Session cookie", "secret", "dic"),
    Field("tracker.dic.dottorrents_dir", "Torrent output directory", "path", "dic",
          "Only if DIC's .torrent files should go somewhere of their own. Blank uses the one under Paths."),

    # --- Checker ------------------------------------------------------
    Field("checker.tracker_budget", "Calls allowed per window", "int", "checker", minimum=1),
    Field("checker.tracker_budget_window", "Window length (seconds)", "int", "checker", minimum=10),
    Field("checker.tracker_call_delay", "Minimum gap between calls (seconds)", "float", "checker", minimum=0),
    Field("checker.tracker_switch_delay", "Pause when switching tracker (seconds)", "float", "checker", minimum=0),
    Field("checker.failure_threshold", "Failures before a tracker is benched", "int", "checker", minimum=1),
    Field("checker.cooldown_seconds", "Bench duration (seconds)", "int", "checker", minimum=0),

    # --- What gets checked --------------------------------------------
    # These filter releases; the ones above bound how hard a tracker is hit.
    # Filed together under "Tracker budget" they read as rate limiting, and an
    # album ignored for its track count has nothing to do with rate limiting.
    Field("checker.min_tracks", "Ignore albums with fewer tracks than", "int", "scanning", "0 disables.",
          minimum=0),
    Field("checker.min_date", "Ignore releases before", "text", "scanning", "YYYY-MM-DD. Blank disables.",
          placeholder="2025-01-01"),
    Field("checker.max_date", "Ignore releases after", "text", "scanning", "YYYY-MM-DD. Blank disables.",
          placeholder="2026-12-31"),
    Field("checker.min_confidence", "Minimum request match confidence", "float", "scanning",
          "How closely a Deezer release must match a request before it counts as a fill. Artist and title "
          "must also clear their own thresholds.", minimum=0.0, maximum=1.0),
    # --- What reaches the queue ---------------------------------------
    # One dropdown whose every option is a whole sentence about a situation,
    # and one checkbox. The page fills the choices in from the trackers you
    # actually have configured.
    Field("checker.queue_when", "Queue a release when it is", "choice", "queue",
          "Everything a check found is kept either way. This decides which of it is worth acting on.",
          choices=QUEUE_CHOICES, labels=QUEUE_LABELS),
    Field("checker.queue_requests_too", "Also queue anything that fills an open request", "bool", "queue",
          "Even when it does not match the rule above. An open request is a reason to upload on its own."),

    Field("checker.request_recheck_after_days", "Re-check a request after", "int", "queue",
          "Days. A request already looked up is skipped inside this window -- what Deezer has and what the "
          "request wants barely move, and asking again costs a tracker call and a Deezer search for an "
          "answer you already have. 0 never re-checks one that has an answer.",
          minimum=0, maximum=3650),

    Field("checker.state_dir", "Scan history directory", "path", "paths",
          "Which albums and requests have already been checked, so a rescan does not spend tracker budget "
          "asking again."),

    # --- Linking ------------------------------------------------------
    Field("linking.enabled", "Hardlink releases per tracker", "bool", "linking"),
    Field("linking.link_dir", "Seeding directory", "path", "linking", "Must share a filesystem with downloads."),
    Field("linking.method", "Method", "choice", "linking",
          "Hardlink unless your client cannot follow them. Copy means a second full copy of every release.",
          choices=("hardlink", "symlink", "copy")),
    Field("linking.per_tracker_dirs", "Separate folder per tracker", "bool", "linking"),
    Field("linking.fallback_to_copy", "Fall back to a real copy if linking fails", "bool", "linking",
          "Leave off so a cross-filesystem mistake fails loudly instead of doubling disk usage."),

    # --- Images -------------------------------------------------------
    Field("image.image_uploader", "General images", "choice", "images",
          "Anything that is neither the cover nor a spectral.",
          choices=("ptscreens", "oeimg", "catbox", "imgbb", "imgbox")),
    Field("image.cover_uploader", "Cover art", "choice", "images",
          "The image the group is created with. Whichever host you pick has to still be serving it years "
          "from now.", choices=("ptscreens", "oeimg", "catbox", "imgbb", "imgbox")),
    Field("image.specs_uploader", "Spectrals", "choice", "images",
          "Linked from the torrent description as proof of what was checked.",
          choices=("ptscreens", "oeimg", "catbox", "imgbb", "imgbox")),
    Field("image.ptscreens_key", "ptscreens key", "secret", "images", test="image:ptscreens"),
    Field("image.oeimg_key", "OnlyImage key", "secret", "images",
          "onlyimage.org. The key is under your profile there.", test="image:oeimg"),
    Field("image.imgbb_key", "imgbb key", "secret", "images", test="image:imgbb"),
    Field("image.auto_compress_cover", "Compress covers automatically", "bool", "images",
          "For hosts that refuse a large file."),
    Field("image.remove_auto_downloaded_cover_image", "Delete covers lox downloaded", "bool", "images",
          "When a release arrives with no cover, one is fetched to upload with. This removes it again "
          "afterwards instead of leaving it in the release folder."),

    # --- Verification sources ----------------------------------------
    Field("metadata.discogs_token", "Discogs token", "secret", "metadata",
          "discogs.com → Settings → Developers.", test="discogs"),
    Field("metadata.apple_music_token", "Apple Music developer token", "secret", "metadata", test="apple"),
    Field("metadata.qobuz.app_id", "Qobuz app ID", "text", "metadata"),
    Field("metadata.qobuz.user_auth_token", "Qobuz auth token", "secret", "metadata",
          "Tested together with the app ID.", test="qobuz"),
    Field("metadata.tidal.token", "Tidal token", "secret", "metadata", test="tidal"),

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
    Field("upload.upload_to_seedbox", "Hand finished uploads to a torrent client", "bool", "upload",
          "The switch for the whole feature. Off, nothing reaches a client whatever is set up under "
          "Files → Torrent client."),
    Field("upload.simultaneous_threads", "Files worked on at once", "int", "upload",
          "How many tracks are read, hashed or drawn as spectrals in parallel. Higher is faster until the "
          "disk becomes the limit.", minimum=1, maximum=16),
    Field("upload.debug", "Debug mode", "bool", "debug",
          "Verbose logging, shown below. Credentials are redacted before anything is written."),
    Field("upload.debug_tracker_connection", "Also log tracker requests and responses", "bool", "debug", "Noisy."),
    Field("logging.directory", "Log directory", "path", "debug",
          "Defaults to a logs folder beside settings.toml."),
    Field("logging.max_file_bytes", "Maximum size per log file", "bytes", "debug",
          "A log is rotated when it reaches this size.", minimum=65536),
    Field("logging.max_total_bytes", "Maximum total log size", "bytes", "debug",
          "Older files are deleted to stay under it.", minimum=1048576),
    Field("upload.compression.flac_compression_level", "FLAC compression level", "int", "upload",
          "8 is smallest and slowest, 0 the reverse. Lossless either way, and it only applies where lox "
          "re-encodes a file — recompressing a release, or repairing a corrupt track.",
          minimum=0, maximum=8),
    Field("upload.compression.compress_spectrals", "Compress spectrals before upload", "bool", "upload",
          "Smaller images to the host. Turn off if the compression is costing detail you need to see."),
    Field("upload.compression.use_upc_as_catno", "Use the barcode as the catalogue number", "bool", "upload",
          "When the release has no catalogue number of its own."),
    Field("upload.search.blacklisted_genres", "Genres to drop", "list", "upload",
          "Genres removed from every scrape before the metadata form. One per line."),
    Field("upload.requests.check_requests", "Offer to fill a request", "bool", "requests",
          "Searches the tracker for open requests this release would fill, and offers to attach one."),
    Field("upload.requests.always_ask_for_request_fill", "Ask even when nothing matched", "bool", "requests",
          "So a request the search missed can still be filled by pasting its id."),
    Field("upload.requests.last_minute_dupe_check", "Re-check for duplicates before posting", "bool", "requests",
          "One more search immediately before the upload, for races with another uploader."),

    # --- Formatting ---------------------------------------------------
    Field("upload.formatting.folder_template", "Folder template", "text", "formatting",
          "Takes {artists}, {title}, {year}, {source}, {format}, {encoding} and {label}. "
          "A field the release does not have is dropped along with the brackets around it.",
          placeholder="{artists} - {title} ({year}) [{source} {format}]"),
    Field("upload.formatting.file_template", "File template", "text", "formatting",
          "Takes {tracknumber}, {artist}, {title}, {album} and {date} — the track's own tags, not the "
          "release's.",
          placeholder="{tracknumber}. {artist} - {title}"),
    Field("upload.formatting.one_album_artist_file_template", "File template, single artist", "text", "formatting",
          "Used instead of the template above when the whole release is by one artist."),
    Field("upload.formatting.no_artist_in_filename_if_only_one_album_artist",
          "Drop the artist from filenames when there is only one", "bool", "formatting"),
    Field("upload.formatting.various_artist_threshold", "Artists before a release is Various", "int", "formatting",
          minimum=2),
    Field("upload.formatting.various_artist_word", "Various Artists label", "text", "formatting"),
    Field("upload.formatting.strip_useless_versions", "Strip redundant version suffixes", "bool", "formatting"),
    Field("upload.formatting.lowercase_cover", "Lowercase the cover filename", "bool", "formatting"),
    Field("upload.formatting.blacklisted_substitution", "Replace illegal characters with", "text", "formatting",
          "What stands in for characters a filesystem will not accept.", placeholder="_"),

    # --- Descriptions -------------------------------------------------
    Field("upload.description.icons_in_descriptions", "Source icons", "bool", "description"),
    Field("upload.description.include_tracklist_in_t_desc", "Tracklist in the torrent description",
          "bool", "description"),
    Field("upload.description.bitrates_in_t_desc", "Per-track bitrates", "bool", "description"),
    Field("upload.compression.lma_comment_in_t_desc", "Lossy-master notes", "bool", "description",
          "Includes the approval comment when a release is flagged as lossy mastered."),
    Field("upload.description.fullwidth_replacements", "Full-width lookalikes for illegal characters",
          "bool", "description", "Uses ： and ？ instead of dropping a colon or a question mark."),
    # Deliberately not here: upload.description.copy_uploaded_url_to_clipboard.
    # It copies to the clipboard of the machine the *server* runs on, which for
    # anyone using this page is not the machine they are sitting at. A setting
    # that cannot do anything from the UI does not belong on it. It still works
    # from config.toml for the command line, where it makes sense.

    # --- Paths --------------------------------------------------------
    Field("directory.download_directory", "Download directory", "path", "paths",
          "Where releases live. Created if missing. Overrides LOX_DOWNLOAD_DIR."),
    Field("directory.dottorrents_dir", "Torrent output directory", "path", "paths",
          "Where .torrent files are written. Created if missing. Overrides LOX_TORRENTS_DIR."),
    Field("directory.tmp_dir", "Spectral scratch directory", "path", "paths",
          "Working space for the spectral images. Each upload clears its own when it finishes."),
    Field("directory.clean_tmp_dir", "Empty the scratch directory at startup", "bool", "paths",
          "Clears anything a run that was interrupted left behind."),
)

FIELDS_BY_KEY: dict[str, Field] = {f.key: f for f in FIELDS}


def sections_with_fields() -> list[dict[str, Any]]:
    """Return sections, each carrying its own fields, in display order."""
    out = []
    for section in SECTIONS:
        fields = [f.as_dict() for f in FIELDS if f.section == section.id]
        # A section with no editable fields but a test still earns its place:
        # the torrent clients are declared in config.toml, and hiding the
        # section hid the only way to check they answer.
        if fields or section.test:
            out.append({**section.as_dict(), "fields": fields})
    return out
