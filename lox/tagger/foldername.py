import contextlib
import os
import re
import shutil
from copy import copy
from string import Formatter

import asyncclick as click

from lox import cfg
from lox.common import strip_template_keys
from lox.common.prompts import confirm as ask_confirm
from lox.common.prompts import edit as ask_edit
from lox.constants import (
    BLACKLISTED_CHARS,
    BLACKLISTED_FULLWIDTH_REPLACEMENTS,
)
from lox.errors import UploadError


async def rename_folder(path, metadata, auto_rename, check=True):
    """
    Create a revised folder name from the new metadata and present it to the
    user. Have them decide whether or not to accept the folder name.
    Then offer them the ability to edit the folder name in a text editor
    before the renaming occurs.
    For scene releases, the name of the original folder is kept untouched, and
    the folder is copied to the download folder.
    """
    old_base = os.path.basename(path)
    new_base = generate_folder_name(metadata)
    if metadata["scene"]:
        new_base = old_base
        auto_rename = True

    # Shown even when the name is already right. Skipping the step silently is
    # why an upload could go from the metadata straight to "upload the
    # torrent?" with no sign that the folder had been looked at.
    if check:
        click.secho("\nRenaming folder...", fg="cyan", bold=True)
        click.echo(f"Old folder name        : {old_base}")
        click.echo(f"New pending folder name: {new_base}")

    if check and old_base != new_base:
        user_rename_choice = cfg.upload.yes_all or await ask_confirm(
            click.style("\nWould you like to replace the original folder name?",
                        fg="magenta"),
            default=True,
        )

        new_base = (
            await _edit_folder_interactive(new_base, auto_rename)
            if auto_rename or user_rename_choice
            else old_base
        )
    elif check:
        click.secho("The folder is already named correctly.", fg="green")

    # Renamed where it stands, not moved into the download directory. With
    # per-tracker linking the release lives in <link_dir>/<TRACKER>/, and
    # relocating it from there tore the seeding layout apart: the files ended
    # up back in the download directory, the client was still told to seed from
    # the tracker folder, and the emptied tracker folder was then deleted by the
    # cleanup below.
    new_path = os.path.join(os.path.dirname(os.path.abspath(path)), new_base)
    if os.path.isdir(new_path) and not os.path.samefile(path, new_path):
        if not check or cfg.upload.yes_all or await ask_confirm(
            click.style(
                f"A folder already exists with the new folder name '{new_path}', "
                "would you like to replace it?",
                fg="magenta",
                bold=True,
            ),
            default=True,
        ):
            shutil.rmtree(new_path)
        else:
            raise UploadError("New folder name already exists.")
    new_path_dirname = os.path.dirname(new_path)
    if not os.path.exists(new_path_dirname):
        os.makedirs(new_path_dirname)

    if os.path.exists(path) and os.path.exists(new_path) and os.path.samefile(path, new_path):
        click.secho(f"Skipping move, same location already for '{new_path}'", fg="yellow")
    else:
        shutil.move(path, new_path)
        click.secho(f"Moved folder to '{new_path}'.", fg="yellow")
        _remove_empty_source_parent(path, new_path)

    # Also rename spectrals folder in TMP_DIR if it exists
    if cfg.directory.tmp_dir and os.path.exists(cfg.directory.tmp_dir):
        tmp_old_specs_path = os.path.join(cfg.directory.tmp_dir, f"spectrals_{old_base}")
        tmp_new_specs_path = os.path.join(cfg.directory.tmp_dir, f"spectrals_{new_base}")

        if not os.path.exists(tmp_old_specs_path):
            pass  # No spectrals folder exists, nothing to rename
        elif os.path.exists(tmp_new_specs_path) and os.path.samefile(tmp_old_specs_path, tmp_new_specs_path):
            click.secho(f"Skipping move, same location already for '{tmp_new_specs_path}'", fg="yellow")
        else:
            shutil.move(tmp_old_specs_path, tmp_new_specs_path)
            click.secho(f"Moved temporary spectrals folder to '{tmp_new_specs_path}'.", fg="yellow")

    return new_path


def _protected_roots() -> set[str]:
    """Directories that exist because they are configured, not because of a release.

    A release folder is disposable; the directory it sits in is not. Deleting
    one takes the next upload's destination with it.
    """
    roots = [cfg.directory.download_directory, cfg.directory.tmp_dir, cfg.linking.link_dir]
    if cfg.linking.link_dir and cfg.linking.per_tracker_dirs:
        # <link_dir>/<TRACKER> is created per tracker and is equally not ours to remove.
        with contextlib.suppress(OSError):
            roots += [
                os.path.join(cfg.linking.link_dir, name)
                for name in os.listdir(cfg.linking.link_dir)
                if os.path.isdir(os.path.join(cfg.linking.link_dir, name))
            ]
    return {os.path.abspath(r) for r in roots if r}


def _remove_empty_source_parent(path: str, new_path: str) -> None:
    """Tidy away the directory a moved release left behind, if it was only a wrapper.

    This deleted ``<link_dir>/RED`` in production: the release was moved out of
    the tracker's seeding directory, that directory was then empty, and it went
    too -- so the client had nothing to seed from and the next upload had
    nowhere to link into. It only ever meant to clean up a wrapper folder a
    download arrived in, so it now refuses to touch a configured root, and does
    nothing at all when the move stayed inside the same parent.
    """
    source_parent = os.path.abspath(os.path.dirname(path))
    if source_parent == os.path.abspath(os.path.dirname(new_path)):
        return
    if source_parent in _protected_roots():
        return
    if not os.path.isdir(source_parent) or os.listdir(source_parent):
        return
    try:
        os.rmdir(source_parent)
        click.secho(f"Removed empty source parent directory '{source_parent}'.", fg="yellow")
    except OSError as e:
        click.secho(f"Could not remove '{source_parent}': {e}", fg="yellow")


def generate_folder_name(metadata):
    """
    Fill in the values from the folder template using the metadata, then strip
    away the unnecessary keys.
    """
    metadata = {**metadata, **{"artists": _compile_artist_str(metadata["artists"])}}
    template = cfg.upload.formatting.folder_template
    keys = [fn for _, fn, _, _ in Formatter().parse(template) if fn]
    for k in keys.copy():
        if not metadata.get(k):
            template = strip_template_keys(template, k)
            keys.remove(k)
    sub_metadata = _fix_format(metadata, keys)
    return template.format(**{k: _sub_illegal_characters(sub_metadata[k]) for k in keys})


def _compile_artist_str(artist_data):
    """Create a string to represent the main artists of the release."""
    artists = [a[0] for a in artist_data if a[1] == "main"]
    if len(artists) > cfg.upload.formatting.various_artist_threshold:
        return cfg.upload.formatting.various_artist_word
    c = ", " if len(artists) > 2 or "&" in "".join(artists) else " & "
    return c.join(sorted(artists))


def _sub_illegal_characters(stri):
    if cfg.upload.description.fullwidth_replacements:
        for char, sub in BLACKLISTED_FULLWIDTH_REPLACEMENTS.items():
            stri = str(stri).replace(char, sub)
    return re.sub(BLACKLISTED_CHARS, cfg.upload.formatting.blacklisted_substitution, str(stri))


def _fix_format(metadata, keys):
    """
    Add abbreviated encoding to format key when the format is not 'FLAC'.
    Helpful for 24 bit FLAC and MP3 320/V0 stuff.

    So far only 24 bit FLAC is supported, when I fix the script for MP3 i will
    add MP3 encodings.
    """
    sub_metadata = copy(metadata)
    if "format" in keys:
        if metadata["format"] == "FLAC" and metadata["encoding"] == "24bit Lossless":
            sub_metadata["format"] = "24bit FLAC"
        elif metadata["format"] == "MP3":
            enc = re.sub(r" \(VBR\)", "", str(metadata["encoding"]))
            sub_metadata["format"] = f"MP3 {enc}"
            if metadata["encoding_vbr"]:
                sub_metadata["format"] += " (VBR)"
        elif metadata["format"] == "AAC":
            enc = re.sub(r" \(VBR\)", "", metadata["encoding"])
            sub_metadata["format"] = f"AAC {enc}"
            if metadata["encoding_vbr"]:
                sub_metadata["format"] += " (VBR)"
    return sub_metadata


async def _edit_folder_interactive(foldername, auto_rename):
    """Allow the user to edit the pending folder name."""
    if auto_rename:
        return foldername
    if not cfg.upload.yes_all and not await ask_confirm(
        click.style("Is the new folder name acceptable? ([n] to edit)", fg="magenta"),
        default=True,
    ):
        newname = await ask_edit(foldername, editor=cfg.upload.default_editor)
        while True:
            if newname is None:
                return foldername
            elif re.search(BLACKLISTED_CHARS, newname):
                if not await ask_confirm(
                    click.style(
                        "Folder name contains invalid characters, retry?",
                        fg="magenta",
                        bold=True,
                    ),
                    default=True,
                ):
                    # Abort the upload rather than exit() the process -- this
                    # runs inside a web request, where exit() takes the server
                    # down with it.
                    raise click.Abort
            else:
                return newname.strip().replace("\n", "")
            newname = await ask_edit(foldername, editor=cfg.upload.default_editor)
    return foldername
