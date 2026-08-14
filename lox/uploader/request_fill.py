"""Given a release being uploaded, find open requests it fills.

The mirror image of :mod:`lox.checker.deezer_requests`, which asks the
opposite question: given an open request, which Deezer release fills it?
"""

from typing import TYPE_CHECKING, Any
from urllib import parse

import asyncclick as click
import humanfriendly

from lox import cfg
from lox.errors import RequestError

if TYPE_CHECKING:
    from lox.trackers.base import BaseGazelleApi


async def check_requests(gazelle_site: "BaseGazelleApi", searchstrs: list[str]) -> int | None:
    """Search for requests on site and offer a choice to fill one.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Search strings to find requests.

    Returns:
        Request ID if user chooses to fill one, None otherwise.
    """
    results = await get_request_results(gazelle_site, searchstrs)
    print_request_results(gazelle_site, results, " / ".join(searchstrs))
    if not results and not cfg.upload.requests.always_ask_for_request_fill:
        return None

    # Asked again when the id turns out not to exist, rather than abandoning
    # the upload. This runs after the cover has been uploaded to an image host
    # and the spectrals generated, immediately before the release is posted --
    # so a typo in a pasted request URL used to throw all of that away and post
    # nothing, which is a lot to lose to a wrong digit.
    while True:
        request_id = await _prompt_for_request_id(gazelle_site, results)
        if not request_id:
            return None
        confirmation = await _confirm_request_id(gazelle_site, request_id)
        if confirmation is True:
            return request_id
        if confirmation is False:
            return None


async def get_request_results(gazelle_site: "BaseGazelleApi", searchstrs: list[str]) -> list[dict[str, Any]]:
    """Get the request results from gazelle site.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Search strings to find requests.

    Returns:
        List of request results.
    """
    results = []
    for searchstr in searchstrs:
        response = await gazelle_site.request("requests", {"search": searchstr})
        for req in (response or {}).get("results") or []:
            if req not in results:
                results.append(req)
    # Read with .get: a request without the field raised KeyError here, and
    # this runs after the torrent has been built, so losing it costs the run.
    # Anything that does not say what it is, is not assumed to be music.
    return [item for item in results if isinstance(item, dict) and item.get("categoryName") == "Music"]


def print_request_results(gazelle_site, results, searchstr):
    """Print all the request search results. Could use a table in the future."""
    if not results:
        click.secho(
            f"\nNo requests were found on {gazelle_site.site_string}",
            fg="green",
            nl=False,
        )
        click.secho(f" (searchstrs: {searchstr})", bold=True)
    else:
        click.secho(
            f"\nRequests were found on {gazelle_site.site_string}: ",
            fg="green",
            nl=False,
        )
        click.secho(f" (searchstrs: {searchstr})", bold=True)
        for r_index, r in enumerate(results):
            try:
                url = gazelle_site.request_url(r["requestId"])
                # User doesn't get to pick a zero index
                click.echo(f" {r_index + 1:02d} >> {url} | ", nl=False)
                names = [a["name"] for a in r["artists"][0]]
                r["artist"] = "Various Artists" if len(names) > 3 else " & ".join(names)
                click.secho(f"{r['artist']}", fg="cyan", nl=False)
                click.secho(f" - {r['title']} ", fg="cyan", nl=False)
                click.secho(f"({r['year']}) [{r['releaseType']}] ", fg="yellow")
                click.secho(f"Requirements: {' or '.join(r['bitrateList'])} / ", nl=False)
                click.secho(f"{' or '.join(r['formatList'])} / ", nl=False)
                click.secho(f"{' or '.join(r['mediaList'])} / ")
            except (KeyError, TypeError):
                continue


def _print_request_details(gazelle_site, req):
    """Print request details.

    Read with .get throughout: the two trackers do not return the same set of
    fields, and a request missing one used to raise KeyError in the middle of
    the fill prompt -- which took the whole upload with it.
    """
    click.secho("\nSelected Request:")
    click.secho(gazelle_site.request_url(req.get("requestId", "")))
    click.secho(f" {req.get('artist', '')}", fg="cyan", nl=False)
    click.secho(f" - {req.get('title', '')} ", fg="cyan", nl=False)
    click.secho(f"({req.get('year', '')})", fg="yellow")
    click.secho(f" - {req.get('requestorName', '')} ", fg="cyan", nl=False)

    bounty = req.get("totalBounty") or req.get("bounty") or 0
    try:
        bounty_str = humanfriendly.format_size(int(bounty), binary=True)
    except (TypeError, ValueError):
        bounty_str = str(bounty)
    click.secho(bounty_str, fg="cyan")

    click.secho(f"Allowed Bitrate: {' | '.join(req.get('bitrateList') or ['Any'])}")
    click.secho(f"Allowed Formats: {' | '.join(req.get('formatList') or ['Any'])}")
    media = list(req.get("mediaList") or [])
    if "CD" in media:
        media.remove("CD")
        media.append("CD " + str(req.get("logCue", "")))
    click.secho(f"Allowed   Media: {' | '.join(media or ['Any'])}")
    click.secho("Description:", fg="cyan")
    description = (req.get("bbDescription") or req.get("description") or "").splitlines(True)

    # Should probably be refactored out and a setting.
    line_limit = 5
    num_lines = len(description)
    if num_lines > line_limit:
        description = "".join(description[:line_limit]) + f"...{num_lines - line_limit} more lines..."
    else:
        description = "".join(description)
    click.echo(description)


def _id_from_url(text: str) -> int | None:
    """The request id in a requests.php URL, or None if it is not one.

    Matched on the path and the query rather than on the site's own base URL:
    that comparison was ``text.lower().startswith(base_url + "/requests.php")``,
    which fails on a URL pasted with different capitalisation, with a www., or
    from the tracker's other domain -- and a URL that fails it falls through
    every branch, which is how this prompt used to loop forever.
    """
    try:
        parsed = parse.urlparse(text.strip())
    except ValueError:
        return None
    if "requests.php" not in parsed.path.lower():
        return None
    ids = parse.parse_qs(parsed.query).get("id") or []
    if not ids or not str(ids[0]).strip().isdigit():
        return None
    return int(str(ids[0]).strip())


async def _prompt_for_request_id(gazelle_site, results):
    """Ask which request to fill, if any.

    Returns:
        A request id, or None to fill nothing.
    """
    while True:
        request_id = await click.prompt(
            click.style("\nFill a request? Choose from results, paste a url, or do[n]t.", fg="magenta"),
            default="N",
        )
        request_id = str(request_id or "").strip()

        from_url = _id_from_url(request_id)
        if from_url is not None:
            return from_url

        if request_id.isdigit():
            raw_input = int(request_id)
            list_index = max(0, raw_input - 1)  # 1-based → 0-based, clamp to 0
            if list_index < len(results):
                chosen = int(results[list_index]["requestId"])
                # Say which one, out loud. A bare number is a row number here,
                # not a request id, and the two are indistinguishable on sight
                # -- so typing the id of the request you meant quietly selects
                # a different one. Naming it gives the confirmation that
                # follows something to disagree with.
                click.secho(f"Row {raw_input} of the results above: request {chosen}.", fg="cyan")
                return chosen
            click.echo(f"No row {raw_input} in the results; reading it as a request id")
            return raw_input

        if request_id.lower().startswith("n") or not request_id:
            click.echo("Not filling a request")
            return None

        # Every branch missed. Saying so is the difference between a prompt you
        # can recover from and one that repeats forever with no explanation,
        # which is what this did to an upload that could only then be cancelled.
        click.secho(
            f"Did not understand {request_id!r}. Give a row number from the list, a request URL, "
            "or n to fill nothing.",
            fg="red",
        )


async def _confirm_request_id(gazelle_site: "BaseGazelleApi", request_id: str | int) -> bool | None:
    """Show the request and ask whether to fill it.

    Args:
        gazelle_site: The tracker API instance.
        request_id: The request ID to confirm.

    Returns:
        True to fill it, False to fill nothing, or None when there is no such
        request -- which is a reason to ask again, not a reason to stop.
    """
    try:
        req = await gazelle_site.request("request", {"id": request_id})
        artists = ((req.get("musicInfo") or {}).get("artists")) or []
        req["artist"] = (
            "Various Artists" if len(artists) > 3
            else " & ".join(a.get("name", "") for a in artists if isinstance(a, dict))
        )
    except RequestError:
        # Not an abort. The release is built and about to be posted; losing it
        # over a wrong request id is a worse answer than asking again.
        click.secho(f"There is no request {request_id} on {gazelle_site.site_string}.", fg="red")
        return None
    _print_request_details(gazelle_site, req)
    if cfg.upload.yes_all:
        return True

    while True:
        answer = await click.prompt(
            click.style("\nAre you sure you would you like to fill this request [Y]es, [n]o", fg="magenta"),
            default="Y",
        )
        # Indexing [0] straight off the answer raised IndexError on an empty
        # one, which aborted the upload after the torrent had been built.
        resp = str(answer or "y").strip().lower()[:1] or "y"
        if resp == "y":
            return True
        if resp == "n":
            click.secho("Not filling this request", fg="red")
            return False
        click.secho(f"Answer y or n, not {answer!r}.", fg="red")
