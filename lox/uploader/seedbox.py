import argparse
import collections
import os
import posixpath

import anyio
import asyncclick as click

from lox import cfg
from lox.clients import TorrentClient, TorrentClientGenerator
from lox.config.validations import Seedbox


def _resolve_shell_path(remote_folder: str, extra_args: list[str]) -> str:
    """Resolve the effective download path, respecting --sftp-path-override.

    Args:
        remote_folder: The base remote directory path.
        extra_args: Extra CLI arguments that may contain --sftp-path-override.

    Returns:
        Effective shell path for the torrent client.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--sftp-path-override", type=str, default=None)
    known_args, _ = parser.parse_known_args(extra_args)
    override = known_args.sftp_path_override
    if not override:
        return remote_folder
    if override.startswith("@"):
        return posixpath.join(override.removeprefix("@"), remote_folder.removeprefix("/"))
    return override


async def _rclone_upload_folder(seedbox: Seedbox, remote_folder: str, path: str) -> None:
    """Upload a local folder to a rclone remote.

    Args:
        seedbox: Seedbox config providing the rclone remote URL and extra args.
        remote_folder: Destination directory on the remote.
        path: Local folder path to upload.
    """
    remote_path = posixpath.join(remote_folder, os.path.basename(path))
    commands = ["rclone", "copy", path, f"{seedbox.url}:{remote_path}", *seedbox.extra_args]
    click.secho(f"Starting Rclone upload to {seedbox.url}:{remote_folder}", fg="cyan")
    click.secho(f"Executing: {' '.join(commands)}", fg="yellow")
    result = await anyio.run_process(commands)
    if result.returncode == 0:
        click.secho(f"Rclone upload successful: {path} to {seedbox.url}:{remote_path}", fg="green")
    else:
        click.secho(f"Rclone upload failed with exit code {result.returncode}", fg="red")


def _expand_tracker(value: str, tracker: str | None) -> str:
    """Substitute {tracker} in a seedbox directory or label.

    Args:
        value: The configured string, possibly containing ``{tracker}``.
        tracker: Tracker code the upload went to.

    Returns:
        The expanded string. With no tracker the placeholder is dropped and any
        resulting doubled or trailing separator is cleaned up.
    """
    if not value or "{tracker}" not in value:
        return value
    if tracker:
        return value.replace("{tracker}", tracker.upper())
    return value.replace("{tracker}", "").replace("//", "/").rstrip("/")


def _default_save_path(torrent_path: str, tracker: str | None) -> str:
    """Work out where the torrent's content actually lives.

    With linking enabled the release was hardlinked to
    ``<link_dir>/<TRACKER>/<release>``, so the client's save path has to be that
    per-tracker directory rather than the download directory. Falls back to the
    download directory when linking is off.

    Args:
        torrent_path: Path of the .torrent file being added.
        tracker: Tracker code the upload went to.

    Returns:
        An absolute save path for the download client.
    """
    if cfg.linking.enabled and cfg.linking.link_dir:
        if cfg.linking.per_tracker_dirs and tracker:
            return os.path.abspath(os.path.join(cfg.linking.link_dir, tracker.upper()))
        return os.path.abspath(cfg.linking.link_dir)
    return os.path.abspath(cfg.directory.download_directory)


async def _add_to_downloader(
    client: TorrentClient,
    shell_path: str,
    torrent_path: str,
    label: str,
    add_paused: bool,
) -> None:
    """Read a torrent file and add it to the download client.

    Args:
        client: Torrent client instance.
        shell_path: Download directory path passed to the client.
        torrent_path: Local path to the .torrent file.
        label: Label to apply in the download client.
        add_paused: Whether to add the torrent in paused state.
    """
    async with await anyio.open_file(torrent_path, "rb") as f:
        torrent = await f.read()
    try:
        client.add_to_downloader(shell_path, torrent, is_paused=add_paused, label=label)
        click.secho("Torrent added to client successfully", fg="green")
    except Exception as e:
        click.secho(f"Failed to add torrent to client: {e}", fg="red")


class UploadManager:
    """Collects upload and seed tasks during a session and executes them all at once.

    Folder tasks are prepended to the queue (run first) so files are present
    on the remote before the corresponding torrents are added to the client.
    """

    def __init__(self) -> None:
        click.secho("Initializing upload managers", fg="cyan")
        self._client_cache: dict[str, TorrentClient] = {}
        for seedbox in cfg.seedbox:
            try:
                if seedbox.torrent_client not in self._client_cache:
                    self._client_cache[seedbox.torrent_client] = TorrentClientGenerator.parse_libtc_url(
                        seedbox.torrent_client
                    )
                click.secho(f"Configured {seedbox.type} uploader to {seedbox.url}", fg="yellow")
            except Exception as e:
                click.secho(f"Failed to configure {seedbox.type} uploader: {e}", fg="red")

        # Each task: (seedbox, local_path, task_type)
        # (seedbox, local path, task type, tracker code)
        self.tasks: collections.deque[tuple[Seedbox, str, str, str | None]] = collections.deque()

    def _client(self, seedbox: Seedbox) -> TorrentClient:
        """Look up the cached torrent client for a seedbox entry.

        Args:
            seedbox: Seedbox config whose torrent_client URL is used as the cache key.

        Returns:
            The cached TorrentClient instance.
        """
        return self._client_cache[seedbox.torrent_client]

    def add_upload_task(self, directory: str, task_type: str, is_flac: bool, tracker: str | None = None) -> None:
        """Queue upload tasks for a path across all configured seedboxes.

        Args:
            directory: Local folder path (for "folder" tasks) or .torrent file path (for "seed" tasks).
            task_type: Either "folder" to transfer files or "seed" to add to the download client.
            is_flac: Whether the release is FLAC; skips seedboxes with flac_only=True if False.
            tracker: Tracker code this upload went to. Selects which seedbox
                entries apply and fills the {tracker} placeholder in their
                directory and label.
        """
        click.secho(f"Preparing upload tasks for: {directory}", fg="cyan")
        for seedbox in cfg.seedbox:
            if seedbox.torrent_client not in self._client_cache:
                continue
            if seedbox.flac_only and not is_flac:
                continue
            if seedbox.tracker and tracker and seedbox.tracker.upper() != tracker.upper():
                continue
            task = (seedbox, directory, task_type, tracker)
            if task in self.tasks:
                continue
            if task_type == "seed":
                self.tasks.append(task)
                click.secho("Added seed task", fg="magenta")
            elif task_type == "folder":
                self.tasks.appendleft(task)
                click.secho("Added folder transfer task", fg="magenta")

    async def execute_upload(self) -> None:
        """Execute all queued upload tasks in order (folders first, then seeds)."""
        if not self.tasks:
            click.secho("No upload tasks to execute", fg="yellow")
            return

        if cfg.upload.dry_run:
            click.secho(f"\n[DRY RUN] Not running {len(self.tasks)} seeding task(s):", fg="yellow", bold=True)
            for seedbox, local_path, task_type, tracker in self.tasks:
                directory = _expand_tracker(seedbox.directory, tracker) or _default_save_path(local_path, tracker)
                label = _expand_tracker(seedbox.label, tracker)
                click.secho(
                    f"  {task_type:6} {os.path.basename(local_path)} -> {seedbox.name or seedbox.type}"
                    f" at {directory}" + (f" [category: {label}]" if label else ""),
                    fg="yellow",
                )
            click.secho("[DRY RUN] Nothing was transferred or added to the download client.\n", fg="yellow", bold=True)
            self.tasks.clear()
            return

        click.secho(f"Executing {len(self.tasks)} upload tasks", fg="cyan")
        for i, (seedbox, local_path, task_type, tracker) in enumerate(self.tasks, 1):
            click.secho(
                f"\nTask {i}/{len(self.tasks)}: {task_type.upper()} - {os.path.basename(local_path)}"
                + (f" [{tracker}]" if tracker else ""),
                fg="cyan",
            )
            directory = _expand_tracker(seedbox.directory, tracker)
            label = _expand_tracker(seedbox.label, tracker)
            try:
                if task_type == "folder":
                    if seedbox.type == "rclone":
                        await _rclone_upload_folder(seedbox, directory, local_path)
                elif task_type == "seed":
                    client = self._client(seedbox)
                    if seedbox.type == "rclone":
                        shell_path = _resolve_shell_path(directory, seedbox.extra_args)
                    else:
                        shell_path = directory or _default_save_path(local_path, tracker)
                    await _add_to_downloader(client, shell_path, local_path, label, seedbox.add_paused)
            except Exception as e:
                click.secho(f"Critical error during task: {e}", fg="red")

        click.secho("\nAll upload tasks processed", fg="green")
        self.tasks.clear()
