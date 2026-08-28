"""Deezer album download engine.

Resolves stream URLs through the private API, streams them to disk while
decrypting the Blowfish stripes, writes tags from the Deezer metadata and drops
a cover into the folder. The output is a plain release folder that the existing
lox upload pipeline can pick up unchanged.
"""

import asyncio
import contextlib
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from typing import Any, Literal

import aiohttp
import msgspec
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSRC

from lox import cfg, debug
from lox.deezer.crypto import CHUNK_SIZE, blowfish_key, decrypt_stripes
from lox.deezer.gw import DeezerGW, DeezerGWError

JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]

COVER_SIZE = 1400
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_EXTENSIONS = {"FLAC": ".flac", "MP3_320": ".mp3", "MP3_128": ".mp3"}


class DownloadError(Exception):
    """Raised when a download cannot be completed."""


def sanitize(name: str, fallback: str = "Unknown") -> str:
    """Make a string safe to use as a path component.

    Args:
        name: The raw name.
        fallback: Value to use when nothing usable is left.

    Returns:
        A filesystem-safe name, trimmed to 180 characters.
    """
    cleaned = _ILLEGAL.sub("_", (name or "").strip()).rstrip(". ")
    return cleaned[:180] or fallback


class TrackDownload(msgspec.Struct):
    """Per-track progress within a job."""

    id: str
    title: str
    artist: str
    number: int
    disc: int
    status: JobStatus = "queued"
    downloaded: int = 0
    size: int = 0
    path: str | None = None
    error: str | None = None
    #: What Deezer actually served, which is not always what was asked for.
    fmt: str = ""


class DownloadJob(msgspec.Struct):
    """One album download."""

    id: str
    album_id: str
    title: str
    artist: str
    cover: str | None = None
    status: JobStatus = "queued"
    tracks: list[TrackDownload] = msgspec.field(default_factory=list)
    folder: str | None = None
    error: str | None = None
    started: float | None = None
    finished: float | None = None
    #: Take whatever quality Deezer will serve, whatever the config says.
    #: Set per download, because "I want this one anyway" is a decision about
    #: one release rather than a setting to go and change and change back.
    allow_lossy: bool = False
    #: What the operator said about a download that came back below FLAC:
    #: "" while nobody has been asked, then "kept" or "discarded".
    decision: str = ""
    # Highest percentage reported so far, so the bar is monotonic even if a
    # size correction would otherwise pull it back.
    _floor: float = 0.0

    @property
    def formats(self) -> list[str]:
        """Every stream quality this job was actually served, best first."""
        order = {name: i for i, name in enumerate(("FLAC", "MP3_320", "MP3_128"))}
        seen = {t.fmt for t in self.tracks if t.fmt}
        return sorted(seen, key=lambda f: order.get(f, 99))

    @property
    def quality(self) -> str:
        """The worst quality served, which is what the release actually is."""
        served = self.formats
        return served[-1] if served else ""

    @property
    def lossy(self) -> bool:
        """True once any track has come back as something other than FLAC.

        Reported the moment the first one lands rather than at the end, so the
        question can be asked while there is still a download to stop.
        """
        return any(t.fmt and t.fmt != "FLAC" for t in self.tracks)

    @property
    def done_count(self) -> int:
        """Number of tracks that finished successfully."""
        return sum(1 for t in self.tracks if t.status == "done")

    @property
    def percent(self) -> float:
        """Overall completion, counted per track rather than per byte.

        A track's size is only known once its download starts, so a total made
        by summing sizes grew every time another track began -- and the bar
        slid backwards each time, 7/10 becoming 7/14. The number of tracks is
        known from the start, so each one is worth the same fixed share and the
        bar only ever moves forwards.
        """
        if not self.tracks:
            return 100.0 if self.status == "done" else 0.0
        share = 0.0
        for track in self.tracks:
            if track.status == "done":
                share += 1.0
            elif track.size:
                share += min(1.0, track.downloaded / track.size)
            elif track.status == "running":
                # Started, but the server has not said how big it is yet.
                # Without this the bar sits still while several tracks are
                # already downloading, then jumps when the first one lands --
                # which is the jumping, not the arithmetic.
                share += 0.05
        # Never goes backwards: a track that has reported some progress cannot
        # report less later, and the denominator is fixed from the start.
        self._floor = max(getattr(self, "_floor", 0.0), min(100.0, share / len(self.tracks) * 100))
        return self._floor

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the web API."""
        return {
            "id": self.id,
            "album_id": self.album_id,
            "title": self.title,
            "artist": self.artist,
            "cover": self.cover,
            "status": self.status,
            "folder": self.folder,
            "error": self.error,
            "percent": round(self.percent, 1),
            "done": self.done_count,
            "total": len(self.tracks),
            "started": self.started,
            "finished": self.finished,
            # What came back, and whether anybody has been asked about it. A
            # release Deezer would only serve as MP3 used to land in the
            # download folder looking exactly like a FLAC one.
            "formats": self.formats,
            "quality": self.quality,
            "lossy": self.lossy,
            "decision": self.decision,
            "allow_lossy": self.allow_lossy,
            "tracks": [
                {
                    "title": t.title,
                    "artist": t.artist,
                    "number": t.number,
                    "status": t.status,
                    "percent": round(t.downloaded / t.size * 100, 1) if t.size else 0.0,
                    "error": t.error,
                }
                for t in self.tracks
            ],
        }


class Downloader:
    """Queue of album downloads served by a pool of workers.

    Jobs are retained after completion so the UI can show history; call
    :meth:`clear_finished` to prune them.
    """

    def __init__(self, gw: DeezerGW, concurrency: int | None = None) -> None:
        """Initialize the downloader.

        Args:
            gw: An authenticated (or authenticatable) private API client.
            concurrency: Simultaneous track downloads. Defaults to config.
        """
        self.gw = gw
        deezer_cfg = getattr(cfg.metadata, "deezer", None)
        self.concurrency = concurrency or getattr(deezer_cfg, "concurrent_downloads", 2) or 2
        self.preferred_format = getattr(deezer_cfg, "preferred_format", "FLAC") or "FLAC"
        self.allow_fallback = bool(getattr(deezer_cfg, "format_fallback", True))
        self.jobs: dict[str, DownloadJob] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._listeners: list[Callable[[DownloadJob], None]] = []
        # Downloads currently in flight, so one can be stopped without stopping
        # the worker carrying it.
        self._running: dict[str, asyncio.Task] = {}
        self._stopping = False

    @property
    def download_dir(self) -> str:
        """Directory that finished releases are written into."""
        deezer_cfg = getattr(cfg.metadata, "deezer", None)
        configured = getattr(deezer_cfg, "download_dir", None) if deezer_cfg else None
        return configured or cfg.directory.download_directory

    def formats(self, allow_lossy: bool = False) -> tuple[str, ...]:
        """Stream qualities to request, honouring the fallback setting.

        Args:
            allow_lossy: Take whatever is available for this one download,
                whatever ``format_fallback`` says. Turning the setting off is
                how you stop lox quietly fetching MP3 for everything; it should
                not also be the thing that stops you fetching one release you
                have decided you want.

        Returns:
            Qualities to ask for, best first.
        """
        order = ("FLAC", "MP3_320", "MP3_128")
        start = order.index(self.preferred_format) if self.preferred_format in order else 0
        if not self.allow_fallback and not allow_lossy:
            return (self.preferred_format,)
        return order[start:]

    def formats_for(self, job: "DownloadJob") -> tuple[str, ...]:
        """Qualities to request for one job."""
        return self.formats(job.allow_lossy)

    def on_update(self, callback: Callable[[DownloadJob], None]) -> None:
        """Register a callback fired whenever a job changes state."""
        self._listeners.append(callback)

    def _notify(self, job: DownloadJob) -> None:
        """Fire update callbacks, ignoring listener failures."""
        for listener in self._listeners:
            with contextlib.suppress(Exception):
                listener(job)

    async def start(self) -> None:
        """Spin up the worker pool if it is not already running."""
        if self._workers:
            return
        self._workers = [asyncio.create_task(self._worker()) for _ in range(max(1, self.concurrency))]

    async def stop(self) -> None:
        """Cancel all workers and wait for them to unwind."""
        self._stopping = True
        for task in self._running.values():
            task.cancel()
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        self._stopping = False

    async def enqueue(self, album_id: str | int, allow_lossy: bool = False) -> DownloadJob:
        """Queue an album for download.

        Args:
            album_id: Deezer album ID.
            allow_lossy: Accept a lower quality than the preferred one for this
                download alone, regardless of ``format_fallback``.

        Returns:
            The created job, already visible in :attr:`jobs`.

        Raises:
            DownloadError: If the album metadata cannot be read.
        """
        try:
            meta = await self.gw.album(album_id)
        except DeezerGWError as e:
            raise DownloadError(f"Could not read album {album_id}: {e}") from e

        job = DownloadJob(
            id=uuid.uuid4().hex[:12],
            album_id=str(album_id),
            title=meta.get("title") or f"Album {album_id}",
            artist=(meta.get("artist") or {}).get("name") or "Unknown Artist",
            cover=meta.get("cover_xl") or meta.get("cover_big") or meta.get("cover"),
            allow_lossy=bool(allow_lossy),
        )
        self.jobs[job.id] = job
        await self.start()
        await self._queue.put(job.id)
        self._notify(job)
        return job

    def cancel(self, job_id: str) -> bool:
        """Mark a queued job as cancelled.

        Running jobs are left alone; only jobs that have not started yet can be
        withdrawn.

        Args:
            job_id: The job to cancel.

        Returns:
            True if the job was cancelled.
        """
        job = self.jobs.get(job_id)
        if not job:
            return False

        # A download in flight is a task of its own, so stopping it does not
        # disturb the other workers. The partial folder is left where it is --
        # deleting half a release on your behalf is not a decision to make for
        # you, and the Downloads list offers a Delete for it.
        task = self._running.get(job_id)
        if task and not task.done():
            task.cancel()
            return True

        if job.status != "queued":
            return False
        job.status = "cancelled"
        self._notify(job)
        return True

    def clear_finished(self) -> int:
        """Drop completed, failed and cancelled jobs. Returns how many went."""
        finished = [k for k, v in self.jobs.items() if v.status in ("done", "failed", "cancelled")]
        for key in finished:
            del self.jobs[key]
        return len(finished)

    async def _worker(self) -> None:
        """Pull job IDs off the queue until cancelled."""
        while True:
            job_id = await self._queue.get()
            try:
                job = self.jobs.get(job_id)
                if job and job.status == "queued":
                    # Run as its own task so one job can be cancelled without
                    # taking down the worker that happens to be carrying it.
                    task = asyncio.ensure_future(self._run_job(job))
                    self._running[job_id] = task
                    try:
                        await task
                    except asyncio.CancelledError:
                        # A cancel aimed at this job, not at the worker: record
                        # it and go back for the next one.
                        if job.status not in ("done", "failed"):
                            job.status = "cancelled"
                            job.finished = time.time()
                            self._notify(job)
                        if self._stopping:
                            raise
                    finally:
                        self._running.pop(job_id, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a bad job must not kill the worker
                job = self.jobs.get(job_id)
                if job:
                    job.status = "failed"
                    job.error = str(e)
                    self._notify(job)
            finally:
                self._queue.task_done()

    async def _run_job(self, job: DownloadJob) -> None:
        """Download every track of one album."""
        job.status = "running"
        job.started = time.time()
        debug.log("download start album=%s %s - %s", job.album_id, job.artist, job.title, level=logging.INFO)
        self._notify(job)

        try:
            songs = await self.gw.album_tracks(job.album_id)
            if not songs:
                raise DownloadError("Album returned no tracks")

            # song.getListData carries TRACK_TOKEN; the album page often does not.
            ids = [str(s.get("SNG_ID") or s.get("id")) for s in songs if s.get("SNG_ID") or s.get("id")]
            detailed = await self.gw.track_data(ids)
            by_id = {str(t.get("SNG_ID")): t for t in detailed}
            songs = [by_id.get(str(s.get("SNG_ID") or s.get("id")), s) for s in songs]

            meta = await self.gw.album(job.album_id)
            job.folder = self._prepare_folder(meta)
            job.tracks = [self._make_track(song, i) for i, song in enumerate(songs, 1)]
            self._notify(job)

            await self._fetch_cover(job, meta)

            semaphore = asyncio.Semaphore(self.concurrency)
            await asyncio.gather(
                *(
                    self._download_track(job, song, track, meta, semaphore)
                    for song, track in zip(songs, job.tracks, strict=True)
                )
            )

            failures = [t for t in job.tracks if t.status != "done"]
            if failures:
                job.status = "failed"
                job.error = f"{len(failures)} of {len(job.tracks)} track(s) failed"
            else:
                job.status = "done"
                self._settle_folder(job)
        except Exception as e:  # noqa: BLE001 - surfaced to the UI via job.error
            job.status = "failed"
            job.error = str(e)
        finally:
            job.finished = time.time()
            debug.log(
                "download %s album=%s tracks=%d/%d %s",
                job.status, job.album_id, job.done_count, len(job.tracks), job.error or "",
                level=logging.INFO,
            )
            self._notify(job)

    def _settle_folder(self, job: DownloadJob) -> None:
        """Rename the folder to match the quality actually downloaded.

        The folder is created before a single stream URL has been resolved, so
        it can only be named for what was asked for -- and it was named
        ``[WEB FLAC]`` unconditionally. A release Deezer served as MP3 then sat
        in a folder claiming to be lossless, which is the one mistake in this
        pipeline a tracker will not forgive.
        """
        quality = job.quality
        if not job.folder or not quality or quality == "FLAC":
            return
        label = "MP3" if quality.startswith("MP3") else quality
        head, tail = os.path.split(job.folder)
        renamed = tail.replace("[WEB FLAC]", f"[WEB {label}]")
        if renamed == tail:
            return
        target = os.path.join(head, renamed)
        try:
            os.rename(job.folder, target)
        except OSError as e:
            debug.log("download: could not rename %s -> %s (%s)", job.folder, target, e, level=30)
            return
        for track in job.tracks:
            if track.path and track.path.startswith(job.folder):
                track.path = os.path.join(target, os.path.relpath(track.path, job.folder))
        job.folder = target

    @staticmethod
    def _make_track(song: dict, index: int) -> TrackDownload:
        """Build the progress record for one song."""
        return TrackDownload(
            id=str(song.get("SNG_ID") or song.get("id") or index),
            title=song.get("SNG_TITLE") or song.get("title") or f"Track {index}",
            artist=song.get("ART_NAME") or song.get("artist", {}).get("name") or "",
            number=int(song.get("TRACK_NUMBER") or index),
            disc=int(song.get("DISK_NUMBER") or 1),
        )

    def _prepare_folder(self, meta: dict) -> str:
        """Create and return the release folder for an album."""
        artist = sanitize((meta.get("artist") or {}).get("name", ""), "Unknown Artist")
        title = sanitize(meta.get("title", ""), "Unknown Album")
        year = (meta.get("release_date") or "")[:4] or "0000"
        folder = os.path.join(self.download_dir, f"{artist} - {title} ({year}) [WEB FLAC]")
        os.makedirs(folder, exist_ok=True)
        return folder

    async def _fetch_cover(self, job: DownloadJob, meta: dict) -> None:
        """Download cover.jpg into the release folder, if one exists."""
        url = meta.get("cover_xl") or meta.get("cover_big") or meta.get("cover")
        if not url or not job.folder:
            return
        session = await self.gw.session()
        with contextlib.suppress(aiohttp.ClientError, OSError):
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    with open(os.path.join(job.folder, "cover.jpg"), "wb") as f:
                        f.write(data)

    async def _download_track(
        self,
        job: DownloadJob,
        song: dict,
        track: TrackDownload,
        meta: dict,
        semaphore: asyncio.Semaphore,
    ) -> None:
        """Resolve, stream, decrypt and tag a single track."""
        async with semaphore:
            track.status = "running"
            self._notify(job)
            try:
                url, fmt = await self.gw.stream_url(song, self.formats_for(job))
                track.fmt = fmt
                extension = _EXTENSIONS.get(fmt, ".flac")
                filename = f"{track.number:02d}. {sanitize(track.title, 'Track')}{extension}"
                path = os.path.join(job.folder or self.download_dir, filename)

                await self._stream_to_disk(job, track, url, path)
                # Mutagen rewrites the whole file to insert tags; keep it off the loop.
                await asyncio.to_thread(self._tag, path, fmt, song, meta, track, job)

                track.path = path
                track.status = "done"
            except (DeezerGWError, DownloadError, aiohttp.ClientError, OSError) as e:
                track.status = "failed"
                track.error = str(e)
            finally:
                self._notify(job)

    async def _stream_to_disk(
        self,
        job: DownloadJob,
        track: TrackDownload,
        url: str,
        path: str,
    ) -> None:
        """Stream an encrypted track to disk, decrypting stripe by stripe."""
        key = blowfish_key(track.id)
        session = await self.gw.session()

        def decrypt_and_write(handle, payload: bytes, index: int) -> int:
            """Decrypt one aligned buffer and write it. Runs off the event loop."""
            plaintext, next_index = decrypt_stripes(payload, key, index)
            handle.write(plaintext)
            return next_index

        async with session.get(url) as resp:
            if resp.status != 200:
                raise DownloadError(f"stream returned HTTP {resp.status}")
            track.size = int(resp.headers.get("Content-Length") or 0)

            buffer = bytearray()
            stripe_index = 0
            last_notify = 0.0
            # Blowfish over a few hundred MB plus the disk writes would stall the
            # event loop, and everything else in the process shares it.
            out = await asyncio.to_thread(open, path, "wb")
            try:
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE * 32):
                    buffer += chunk
                    track.downloaded += len(chunk)
                    aligned = len(buffer) - (len(buffer) % CHUNK_SIZE)
                    if aligned:
                        payload = bytes(buffer[:aligned])
                        del buffer[:aligned]
                        stripe_index = await asyncio.to_thread(decrypt_and_write, out, payload, stripe_index)
                    now = time.monotonic()
                    if now - last_notify > 0.5:
                        last_notify = now
                        self._notify(job)
                if buffer:
                    await asyncio.to_thread(decrypt_and_write, out, bytes(buffer), stripe_index)
            finally:
                await asyncio.to_thread(out.close)

    @staticmethod
    def _tag(path: str, fmt: str, song: dict, meta: dict, track: TrackDownload, job: DownloadJob) -> None:
        """Write tags from the Deezer metadata onto a downloaded file."""
        album_artist = (meta.get("artist") or {}).get("name") or job.artist
        genres = [g.get("name") for g in (meta.get("genres") or {}).get("data", []) if g.get("name")]
        date = meta.get("release_date") or ""
        total_tracks = str(meta.get("nb_tracks") or len(job.tracks))
        cover_path = os.path.join(job.folder or "", "cover.jpg")
        cover = None
        if os.path.exists(cover_path):
            with open(cover_path, "rb") as f:
                cover = f.read()

        if fmt == "FLAC":
            audio = FLAC(path)
            audio["title"] = track.title
            audio["artist"] = song.get("ART_NAME") or album_artist
            audio["albumartist"] = album_artist
            audio["album"] = meta.get("title") or job.title
            audio["tracknumber"] = f"{track.number}/{total_tracks}"
            audio["discnumber"] = str(track.disc)
            audio["date"] = date
            if genres:
                audio["genre"] = genres
            if song.get("ISRC"):
                audio["isrc"] = song["ISRC"]
            if cover:
                picture = Picture()
                picture.type = 3
                picture.mime = "image/jpeg"
                picture.data = cover
                audio.add_picture(picture)
            audio.save()
            return

        audio_id3 = ID3()
        audio_id3.add(TIT2(encoding=3, text=track.title))
        audio_id3.add(TPE1(encoding=3, text=song.get("ART_NAME") or album_artist))
        audio_id3.add(TPE2(encoding=3, text=album_artist))
        audio_id3.add(TALB(encoding=3, text=meta.get("title") or job.title))
        audio_id3.add(TRCK(encoding=3, text=f"{track.number}/{total_tracks}"))
        audio_id3.add(TPOS(encoding=3, text=str(track.disc)))
        if date:
            audio_id3.add(TDRC(encoding=3, text=date))
        if genres:
            audio_id3.add(TCON(encoding=3, text=genres))
        if song.get("ISRC"):
            audio_id3.add(TSRC(encoding=3, text=song["ISRC"]))
        if cover:
            audio_id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        audio_id3.save(path)
