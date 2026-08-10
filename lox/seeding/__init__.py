"""Per-tracker seeding layout.

Uploading the same release to two trackers needs two torrents, and a torrent
client wants each one pointed at its own path. Rather than keep two copies of
the audio, this materializes hardlinked views of a single release folder — the
approach cross-seed uses — so a 500 MB release costs 500 MB no matter how many
trackers it goes to.
"""

from lox.seeding.links import LinkError, LinkResult, link_release, linked_path, unlink_release

__all__ = ["LinkError", "LinkResult", "link_release", "linked_path", "unlink_release"]
