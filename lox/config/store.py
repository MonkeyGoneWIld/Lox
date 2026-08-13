"""Runtime settings edited through the UI.

config.toml stays the bootstrap: the handful of values needed before a web
server exists. Everything else is stored here in ``settings.toml`` next to it,
layered over the parsed config at startup and re-applied whenever the settings
page saves.

Keeping the two files separate means the UI never rewrites hand-authored config
— it only ever touches its own file — and deleting settings.toml reverts to
whatever config.toml says.
"""

import os
import threading
from typing import Any

import msgspec

from lox.config.schema import BOOTSTRAP_KEYS, FIELDS_BY_KEY

SETTINGS_FILENAME = "settings.toml"

SEEDBOX_KEY = "seedbox"
"""Where the torrent-client list is stored. It is a list of tables rather than
a leaf value, so it is handled apart from the dotted-key settings."""


class SettingsError(Exception):
    """Raised when a settings write is rejected."""


def _split(key: str) -> list[str]:
    """Split a dotted settings key into its path components."""
    return key.split(".")


def human_size(count: int) -> str:
    """Render a byte count the way the settings page edits it.

    Uses the largest unit that divides exactly, so 8388608 reads as ``8 MB``
    rather than ``8192 KB`` or ``8.0 MB``.

    Args:
        count: Number of bytes.

    Returns:
        A number and a unit, e.g. ``1 GB``.
    """
    for unit, factor in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if count >= factor and count % factor == 0:
            return f"{count // factor} {unit}"
    return f"{count} B"


def get_value(root: Any, key: str) -> Any:
    """Read a dotted key from a config object or nested dict.

    Args:
        root: The config struct or dict to read from.
        key: Dotted key, e.g. ``metadata.deezer.arl``.

    Returns:
        The value, or None if any part of the path is missing.
    """
    node = root
    for part in _split(key):
        node = node.get(part) if isinstance(node, dict) else getattr(node, part, None)
        if node is None:
            return None
    return node


def set_value(root: Any, key: str, value: Any) -> None:
    """Write a dotted key onto a live config object.

    Args:
        root: The config struct to mutate.
        key: Dotted key.
        value: New value.

    Raises:
        SettingsError: If the path does not exist on the config schema.
    """
    parts = _split(key)
    node = root
    for part in parts[:-1]:
        node = getattr(node, part, None)
        if node is None:
            raise SettingsError(f"{key} has no parent section ({part} is unset)")
    if not hasattr(node, parts[-1]):
        raise SettingsError(f"{key} is not a known setting")
    setattr(node, parts[-1], value)


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    """Turn dotted keys into nested dicts for TOML output."""
    out: dict[str, Any] = {}
    for key, value in flat.items():
        node = out
        parts = _split(key)
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out


def coerce(key: str, value: Any) -> Any:
    """Convert a value from the browser into the type the config expects.

    Empty strings become None so clearing a field in the UI unsets it rather
    than storing a blank.

    Args:
        key: Dotted settings key.
        value: Raw value from the request body.

    Returns:
        The coerced value.

    Raises:
        SettingsError: If the value cannot be represented, or is out of range.
    """
    field = FIELDS_BY_KEY.get(key)
    if field is None:
        raise SettingsError(f"{key} is not an editable setting")

    if field.kind == "bool":
        return bool(value)

    if isinstance(value, str) and not value.strip() and field.kind != "text":
        return None
    if value is None:
        return None

    try:
        if field.kind in ("int", "bytes"):
            value = int(value)
        elif field.kind == "float":
            value = float(value)
        elif field.kind in ("text", "secret", "path", "choice"):
            value = str(value)
            if not value.strip():
                return None if field.kind != "text" else ""
        elif field.kind == "list":
            value = [v.strip() for v in (value if isinstance(value, list) else str(value).split(",")) if v.strip()]
    except (TypeError, ValueError) as e:
        raise SettingsError(f"{field.label}: {value!r} is not a valid {field.kind}") from e

    if field.kind == "choice" and field.choices and value not in field.choices:
        raise SettingsError(f"{field.label}: must be one of {', '.join(field.choices)}")

    def limit(number: float) -> str:
        """Render a bound the same way the field is edited."""
        return human_size(int(number)) if field.kind == "bytes" else str(number)

    if field.minimum is not None and isinstance(value, int | float) and value < field.minimum:
        raise SettingsError(f"{field.label}: must be at least {limit(field.minimum)}")
    if field.maximum is not None and isinstance(value, int | float) and value > field.maximum:
        raise SettingsError(f"{field.label}: must be at most {limit(field.maximum)}")

    return value


class SettingsStore:
    """The UI-managed settings file, and the code that applies it."""

    def __init__(self, directory: str) -> None:
        """Initialize the store.

        Args:
            directory: Directory holding settings.toml. Created if missing.
        """
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, SETTINGS_FILENAME)
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {}
        self.load()

    def load(self) -> dict[str, Any]:
        """Read settings.toml into memory. A missing or broken file yields {}."""
        with self._lock:
            self._values = {}
            if not os.path.exists(self.path):
                return self._values
            try:
                with open(self.path, "rb") as f:
                    nested = msgspec.toml.decode(f.read())
            except (OSError, msgspec.DecodeError):
                return self._values

            def walk(node: dict, prefix: str = "") -> None:
                for key, value in node.items():
                    dotted = f"{prefix}{key}"
                    if isinstance(value, dict):
                        walk(value, f"{dotted}.")
                    else:
                        self._values[dotted] = value

            walk(nested)
            return self._values

    @property
    def values(self) -> dict[str, Any]:
        """The stored overrides, as dotted keys."""
        return dict(self._values)

    def save(self) -> None:
        """Write settings.toml atomically."""
        with self._lock:
            payload = msgspec.toml.encode(_nest(self._values))
            tmp = f"{self.path}.tmp"
            with open(tmp, "wb") as f:
                f.write(payload)
            os.replace(tmp, self.path)

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Validate and store a set of changes.

        Args:
            changes: Dotted key to raw value.

        Returns:
            The coerced values that were stored.

        Raises:
            SettingsError: If a key is unknown, protected, or the value is bad.
        """
        coerced: dict[str, Any] = {}
        for key, raw in changes.items():
            if key in BOOTSTRAP_KEYS:
                raise SettingsError(
                    f"{key} has to be set in config.toml — the server reads it before this page exists."
                )
            coerced[key] = coerce(key, raw)

        with self._lock:
            for key, value in coerced.items():
                if value is None:
                    self._values.pop(key, None)
                else:
                    self._values[key] = value
        self.save()
        return coerced

    @property
    def seedboxes(self) -> list[dict[str, Any]]:
        """The stored torrent-client entries, or an empty list."""
        return list(self._values.get(SEEDBOX_KEY) or [])

    def set_seedboxes(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate and store the torrent-client list.

        Seedboxes are a list of connections rather than a single value, so they
        do not fit the dotted-key scheme the rest of the page uses. They were
        therefore not editable at all: the toggle that injects finished uploads
        into a client was on the page with no way to say which client.

        Args:
            entries: One dict per connection, as the page sends them.

        Returns:
            The cleaned entries that were stored.

        Raises:
            SettingsError: If an entry is not usable.
        """
        from lox.config.validations import Seedbox

        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(entries or [], 1):
            if not isinstance(entry, dict):
                raise SettingsError(f"Torrent client {index} is not a set of fields.")
            payload = {k: v for k, v in entry.items() if v not in (None, "")}
            payload.setdefault("name", f"client {index}")
            try:
                box = msgspec.convert(payload, Seedbox, strict=False)
            except (msgspec.ValidationError, ValueError) as e:
                raise SettingsError(f"Torrent client {index}: {e}") from e
            if box.enabled and not box.torrent_client:
                raise SettingsError(f"Torrent client {index} is enabled but has no connection URL.")
            cleaned.append(msgspec.to_builtins(box))

        with self._lock:
            if cleaned:
                self._values[SEEDBOX_KEY] = cleaned
            else:
                self._values.pop(SEEDBOX_KEY, None)
        self.save()
        return cleaned

    def apply_to(self, cfg: Any) -> list[str]:
        """Layer the stored settings onto a live config object.

        Args:
            cfg: The parsed config to mutate.

        Returns:
            Keys that could not be applied, e.g. a tracker section that does not
            exist in config.toml yet.
        """
        from lox.config.validations import Seedbox

        failed: list[str] = []
        for key, value in self.values.items():
            if key == SEEDBOX_KEY:
                # A list of structs, not a leaf: rebuilt rather than assigned
                # through the dotted-key path the rest of this walks.
                try:
                    cfg.seedbox = [msgspec.convert(entry, Seedbox, strict=False) for entry in value]
                except (msgspec.ValidationError, ValueError):
                    failed.append(key)
                continue
            try:
                set_value(cfg, key, value)
            except SettingsError:
                failed.append(key)
        return failed

    def snapshot(self, cfg: Any, reveal_secrets: bool = False) -> dict[str, Any]:
        """Return the current effective value of every editable setting.

        Secrets are replaced with a placeholder unless explicitly revealed, so
        the settings page can show that a token is set without shipping it back
        to the browser on every load.

        Args:
            cfg: The live config to read effective values from.
            reveal_secrets: Return secret values in full.

        Returns:
            Mapping of dotted key to value, plus a ``__set__`` list naming the
            secrets that currently hold a value.
        """
        values: dict[str, Any] = {}
        secrets_set: list[str] = []
        for key, field in FIELDS_BY_KEY.items():
            current = get_value(cfg, key)
            if field.kind == "secret":
                if current:
                    secrets_set.append(key)
                values[key] = current if reveal_secrets else ("" if not current else None)
            else:
                values[key] = current
        values["__secrets_set__"] = secrets_set
        return values
