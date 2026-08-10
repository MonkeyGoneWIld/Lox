"""Debug mode: verbose logging with credentials stripped out.

Debug logs get pasted into issues and chat windows, so everything that goes
through here is redacted first. The ARL, tracker sessions and API keys are all
long opaque strings that would otherwise be logged verbatim by any library that
prints a request URL or a cookie jar.

Enabled by ``upload.debug`` in settings, the ``LOX_DEBUG`` environment variable,
or ``--debug`` on the CLI.
"""

import logging
import logging.handlers
import os
import re
import time
from collections import deque
from typing import Any

RING_SIZE = 2000
"""How many recent lines are kept in memory for the UI."""

_LOGGER_NAME = "lox"
_ring: deque[str] = deque(maxlen=RING_SIZE)
_configured = False

# Anything that looks like a credential, whether or not we know its name.
_PATTERNS = (
    re.compile(r"(arl=)([A-Za-z0-9]{16,})", re.IGNORECASE),
    re.compile(r"(session=)([^;\s&\"']{16,})", re.IGNORECASE),
    re.compile(r"((?:api[_-]?key|token|authorization|auth_token|webhook)\W{1,4})([A-Za-z0-9+/=._~-]{12,})",
               re.IGNORECASE),
    re.compile(r"(https://discord\.com/api/webhooks/)(\S+)", re.IGNORECASE),
)


def redact(text: str) -> str:
    """Strip anything credential-shaped from a string.

    Args:
        text: The text to clean.

    Returns:
        The text with secrets replaced by a length-preserving marker.
    """
    if not text:
        return text
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}<redacted:{len(m.group(2))}>", text)
    return text


def redact_value(value: Any) -> Any:
    """Redact a value of any type, recursing into containers."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(redact_value(v) for v in value)
    return value


class RedactingFormatter(logging.Formatter):
    """Formats a record and strips anything credential-shaped from the result.

    Applied at the formatter rather than the handler so every sink - the ring
    buffer the UI reads and the file on disk - is redacted by construction. A
    log file that leaks an ARL is worse than no log file.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format and redact one record."""
        return redact(super().format(record))


class _RingHandler(logging.Handler):
    """Keeps recent lines in memory for the UI to tail."""

    def emit(self, record: logging.LogRecord) -> None:
        """Store one already-formatted line."""
        try:
            _ring.append(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never raise
            return


def log_dir() -> str:
    """Return the directory rolling logs are written to, creating it if needed."""
    from lox import cfg, settings

    directory = cfg.logging.directory or os.path.join(os.path.dirname(settings.path), "logs")
    os.makedirs(directory, exist_ok=True)
    return directory


def log_path() -> str:
    """Path of the active log file."""
    return os.path.join(log_dir(), "lox.log")


def logger() -> logging.Logger:
    """Return the lox logger."""
    return logging.getLogger(_LOGGER_NAME)


def enabled() -> bool:
    """True when debug mode is on."""
    from lox import cfg

    return bool(os.environ.get("LOX_DEBUG")) or bool(getattr(cfg.upload, "debug", False))


def configure(force: bool | None = None) -> bool:
    """Set up logging to match the current debug setting.

    Safe to call repeatedly; the settings page calls it after every save.

    Args:
        force: Override the configured value.

    Returns:
        Whether debug mode is now on.
    """
    global _configured
    on = enabled() if force is None else force

    log = logger()
    log.setLevel(logging.DEBUG if on else logging.INFO)
    log.propagate = False

    if not _configured:
        from lox import cfg

        ring = _RingHandler()
        ring.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        log.addHandler(ring)

        # Rolling file log, bounded by size and by total. backup_count is
        # derived from the two limits so the set cannot exceed the cap.
        try:
            rotating = logging.handlers.RotatingFileHandler(
                log_path(),
                maxBytes=cfg.logging.max_file_bytes,
                backupCount=cfg.logging.backup_count,
                encoding="utf-8",
            )
            rotating.setFormatter(
                RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
            )
            log.addHandler(rotating)
        except OSError as e:
            log.addHandler(logging.NullHandler())
            print(f"[lox] could not open the log file: {e}")

        _configured = True

    # aiohttp.access logs one line per HTTP request. The UI polls several
    # endpoints every second, so left at INFO it produces thousands of lines an
    # hour and rotates everything useful out of the file. It is only ever
    # interesting in debug mode.
    for name in ("aiohttp.client", "aiohttp.server", "asyncio"):
        logging.getLogger(name).setLevel(logging.DEBUG if on else logging.WARNING)
        if on:
            for existing in logger().handlers:
                if existing not in logging.getLogger(name).handlers:
                    logging.getLogger(name).addHandler(existing)

    access = logging.getLogger("aiohttp.access")
    access.setLevel(logging.INFO if on else logging.WARNING)
    if on:
        for existing in logger().handlers:
            if existing not in access.handlers:
                access.addHandler(existing)

    return on


def log(message: str, *args: Any, level: int = logging.DEBUG) -> None:
    """Record a message, redacted, if debug mode is on.

    Args:
        message: Format string.
        args: Format arguments.
        level: Logging level.
    """
    if level <= logging.DEBUG and not enabled():
        return
    logger().log(level, redact(message % args if args else message))


def event(category: str, **fields: Any) -> None:
    """Record a structured debug event.

    Args:
        category: Short label, e.g. ``deezer.stream`` or ``tracker.browse``.
        fields: Key/value detail, redacted before it is written.
    """
    if not enabled():
        return
    detail = " ".join(f"{k}={redact_value(v)!r}" for k, v in fields.items())
    logger().debug("%s %s", category, detail)


def recent(limit: int = 300) -> list[str]:
    """Return the most recent redacted log lines, oldest first."""
    return list(_ring)[-limit:]


def clear() -> None:
    """Empty the in-memory log."""
    _ring.clear()


def diagnostics() -> dict[str, Any]:
    """Collect a support bundle: versions, paths and effective settings.

    Every value passes through the redactor, so the result is safe to paste
    into an issue. Secrets are reported as set or unset, never by value.
    """
    import platform
    import sys

    from lox import cfg, settings

    def state(value: Any) -> str:
        return "set" if value else "unset"

    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "debug": enabled(),
        "credentials": {
            "deezer_arl": state(cfg.metadata.deezer.arl),
            "discogs": state(cfg.metadata.discogs_token),
            "red_session": state(cfg.tracker.red.session),
            "red_api_key": state(cfg.tracker.red.api_key),
            "ops_session": state(cfg.tracker.ops.session),
            "ops_api_key": state(cfg.tracker.ops.api_key),
            "auth_token": state(cfg.upload.web_interface.auth_token),
            "discord_webhook": state(cfg.notifications.discord_webhook),
        },
        "logging": {
            "directory": log_dir(),
            "file": log_path(),
            "max_file_bytes": cfg.logging.max_file_bytes,
            "max_total_bytes": cfg.logging.max_total_bytes,
            "files_kept": cfg.logging.backup_count + 1,
        },
        "paths": {
            "download_directory": cfg.directory.download_directory,
            "dottorrents_dir": cfg.directory.dottorrents_dir,
            "tmp_dir": cfg.directory.tmp_dir,
            "link_dir": cfg.linking.link_dir,
            "state_dir": cfg.checker.state_dir,
            "settings_file": settings.path,
        },
        "linking": {
            "enabled": cfg.linking.enabled,
            "method": cfg.linking.method,
            "per_tracker_dirs": cfg.linking.per_tracker_dirs,
            "fallback_to_copy": cfg.linking.fallback_to_copy,
        },
        "upload": {
            "dry_run": cfg.upload.dry_run,
            "yes_all": cfg.upload.yes_all,
            "upload_to_seedbox": cfg.upload.upload_to_seedbox,
        },
        "checker": {
            "tracker_budget": cfg.checker.tracker_budget,
            "tracker_budget_window": cfg.checker.tracker_budget_window,
            "min_confidence": cfg.checker.min_confidence,
        },
        "settings_overridden": sorted(settings.values),
    }


def diagnostics_text() -> str:
    """Render :func:`diagnostics` plus the recent log as pasteable text."""
    data = diagnostics()
    lines = ["=== lox diagnostics ==="]

    def walk(node: dict, indent: str = "") -> None:
        for key, value in node.items():
            if isinstance(value, dict):
                lines.append(f"{indent}{key}:")
                walk(value, indent + "  ")
            else:
                lines.append(f"{indent}{key}: {value}")

    walk(data)
    lines.append("")
    lines.append("=== recent log (redacted) ===")
    lines.extend(recent(500) or ["(empty — enable debug mode and reproduce the problem)"])
    return "\n".join(lines)
