"""Materialize hardlinked per-tracker copies of a release folder.

Layout, matching what the config's ``per_tracker_dirs`` default produces::

    <link_dir>/RED/<release>/...
    <link_dir>/OPS/<release>/...

Every file is a hardlink back to the original, so the bytes exist once on disk
and the torrent client can seed both without a second copy. Hardlinks cannot
cross filesystems; when that happens the behaviour is governed by
``linking.fallback_to_copy`` rather than silently doing something expensive.
"""

import os
import shutil
from typing import Any, Literal

import msgspec

from lox import cfg

Method = Literal["hardlink", "symlink", "copy"]


class LinkError(Exception):
    """Raised when a release cannot be linked into the seeding directory."""


class LinkResult(msgspec.Struct):
    """What linking one release for one tracker produced."""

    tracker: str
    source: str
    destination: str
    method: Method
    files: int
    bytes_linked: int
    bytes_copied: int
    reused: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the web API."""
        return msgspec.to_builtins(self)


def _link_dir() -> str:
    """Return the configured seeding root.

    Raises:
        LinkError: If linking is enabled but no directory is configured.
    """
    directory = cfg.linking.link_dir
    if not directory:
        raise LinkError("linking.link_dir is not set; nothing to link into.")
    return directory


def linked_path(source: str, tracker: str) -> str:
    """Return where a release would be linked for a tracker, without creating it.

    Args:
        source: Path to the original release folder.
        tracker: Tracker code, e.g. ``RED``.

    Returns:
        The destination path.
    """
    base = os.path.basename(os.path.normpath(source))
    root = _link_dir()
    return os.path.join(root, tracker.upper(), base) if cfg.linking.per_tracker_dirs else os.path.join(root, base)


def _same_filesystem(a: str, b: str) -> bool:
    """True when two existing paths sit on the same device."""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _place(src_file: str, dst_file: str, method: Method) -> tuple[Method, int]:
    """Create one file at ``dst_file`` from ``src_file``.

    Returns:
        Tuple of (method actually used, bytes that had to be copied).

    Raises:
        LinkError: If the file cannot be placed and copying is disallowed.
    """
    size = os.path.getsize(src_file)

    if method == "copy":
        shutil.copy2(src_file, dst_file)
        return "copy", size

    try:
        if method == "symlink":
            os.symlink(src_file, dst_file)
        else:
            os.link(src_file, dst_file)
        return method, 0
    except OSError as e:
        if not cfg.linking.fallback_to_copy:
            raise LinkError(
                f"Could not {method} {src_file} -> {dst_file}: {e}. "
                f"Set linking.fallback_to_copy if a real copy is acceptable."
            ) from e
        shutil.copy2(src_file, dst_file)
        return "copy", size


def link_release(source: str, tracker: str, overwrite: bool = False) -> LinkResult:
    """Create a hardlinked view of a release folder for one tracker.

    Args:
        source: Path to the original release folder.
        tracker: Tracker code, e.g. ``RED``.
        overwrite: Replace an existing destination instead of reusing it.

    Returns:
        A LinkResult describing what was created.

    Raises:
        LinkError: If the source is missing, or files cannot be placed.
    """
    if not os.path.isdir(source):
        raise LinkError(f"{source} is not a directory")

    destination = linked_path(source, tracker)
    method: Method = cfg.linking.method

    if os.path.exists(destination):
        if not overwrite:
            existing = sum(len(files) for _root, _dirs, files in os.walk(destination))
            return LinkResult(
                tracker=tracker.upper(),
                source=source,
                destination=destination,
                method=method,
                files=existing,
                bytes_linked=0,
                bytes_copied=0,
                reused=True,
            )
        shutil.rmtree(destination)

    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)

    # Hardlinks cannot cross devices. Check once against the parent rather than
    # discovering it file by file.
    if method == "hardlink" and not _same_filesystem(source, parent):
        if not cfg.linking.fallback_to_copy:
            raise LinkError(
                f"{source} and {parent} are on different filesystems, so hardlinks are impossible. "
                f"Point linking.link_dir at the same volume as your downloads, or enable "
                f"linking.fallback_to_copy."
            )
        method = "copy"

    files = 0
    bytes_linked = 0
    bytes_copied = 0

    try:
        for root, _dirs, filenames in os.walk(source):
            relative = os.path.relpath(root, source)
            target_dir = destination if relative == "." else os.path.join(destination, relative)
            os.makedirs(target_dir, exist_ok=True)
            for filename in filenames:
                src_file = os.path.join(root, filename)
                dst_file = os.path.join(target_dir, filename)
                used, copied = _place(src_file, dst_file, method)
                files += 1
                if copied:
                    bytes_copied += copied
                else:
                    bytes_linked += os.path.getsize(src_file)
                if used == "copy" and method != "copy":
                    method = "copy"
    except (OSError, LinkError):
        # Never leave a half-built folder behind for a torrent client to find.
        shutil.rmtree(destination, ignore_errors=True)
        raise

    if not files:
        shutil.rmtree(destination, ignore_errors=True)
        raise LinkError(f"{source} contains no files")

    return LinkResult(
        tracker=tracker.upper(),
        source=source,
        destination=destination,
        method=method,
        files=files,
        bytes_linked=bytes_linked,
        bytes_copied=bytes_copied,
    )


def unlink_release(source: str, tracker: str) -> bool:
    """Remove a previously linked release folder for one tracker.

    Only ever deletes inside ``linking.link_dir``, so a bad argument cannot
    reach the original release.

    Args:
        source: Path to the original release folder.
        tracker: Tracker code.

    Returns:
        True if a directory was removed.
    """
    destination = os.path.realpath(linked_path(source, tracker))
    root = os.path.realpath(_link_dir())
    # commonpath raises on Windows when the paths sit on different drives, which
    # is itself proof the destination is outside the link dir.
    try:
        inside = os.path.commonpath([root, destination]) == root
    except ValueError:
        inside = False
    if not inside:
        raise LinkError(f"Refusing to remove {destination}: outside linking.link_dir")
    if not os.path.isdir(destination):
        return False
    shutil.rmtree(destination)
    return True
