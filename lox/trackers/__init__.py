from typing import Any
from urllib import parse

import asyncclick as click

from lox import cfg
from lox.trackers import dic, ops, red

# hard coded as it needs to reflect the imports anyway.
tracker_classes = {"RED": red.RedApi, "OPS": ops.OpsApi, "DIC": dic.DICApi}
tracker_url_code_map = {"redacted.sh": "RED", "orpheus.network": "OPS", "dicmusic.com": "DIC"}

#: Where each tracker lives, without having to build an API client to ask.
#:
#: The UI needs these to turn "OPS is missing this" into a link to OPS, and it
#: needs them for trackers there are no credentials for as well -- a browse link
#: is not an API call. Building a client just to read base_url off it would
#: raise for exactly the trackers that most need the link.
tracker_base_urls: dict[str, str] = {
    "RED": "https://redacted.sh",
    "OPS": "https://orpheus.network",
    "DIC": "https://dicmusic.com",
}


def base_url(site_code: str) -> str:
    """The tracker's site root, or "" for a code lox does not know."""
    return tracker_base_urls.get(str(site_code).upper(), "")


def group_url(site_code: str, group_id: Any) -> str:
    """A link to one torrent group on a tracker, or "" if either is unknown."""
    root = base_url(site_code)
    return f"{root}/torrents.php?id={group_id}" if root and group_id else ""


def search_url(site_code: str, terms: str) -> str:
    """A link to a tracker's torrent search for some terms."""
    root = base_url(site_code)
    return f"{root}/torrents.php?searchstr={parse.quote_plus(terms)}" if root and terms else root


def request_url(site_code: str, request_id: Any) -> str:
    """A link to one request on a tracker, or "" if either is unknown."""
    root = base_url(site_code)
    return f"{root}/requests.php?action=view&id={request_id}" if root and request_id else ""


def artist_url(site_code: str, name: str) -> str:
    """A link to a tracker's artist page. Gazelle resolves these by name."""
    root = base_url(site_code)
    return f"{root}/artist.php?artistname={parse.quote_plus(name)}" if root and name else ""


# Which trackers actually have credentials. Mutated in place rather than
# rebound so anything holding a reference keeps seeing the current list; the
# settings page calls refresh_tracker_list() after a save.
#
# Click decorators snapshot this at import for their help text, so CLI choices
# still need a restart to pick up a newly configured tracker. The web UI does not.
tracker_cfg = cfg.tracker
tracker_list: list[str] = []


def refresh_tracker_list() -> list[str]:
    """Recompute the configured tracker list from the live config."""
    tracker_list[:] = cfg.tracker.configured()
    return tracker_list


refresh_tracker_list()


def get_class(site_code):
    "Returns the api class from the tracker string."
    return tracker_classes[site_code]


async def choose_tracker(choices):
    """Allows the user to choose a tracker from choices."""
    while True:
        # Loop until we have chosen a tracker or aborted.
        tracker_input = await click.prompt(
            click.style(f"Your choices are {' , '.join(choices)} or [n]one.", fg="magenta"),
            default=choices[0],
        )
        tracker_input = tracker_input.strip().upper()
        if tracker_input in choices:
            click.secho(f"Using tracker: {tracker_input}", fg="green")
            return tracker_input
        # this part allows input of the first letter of the tracker.
        elif tracker_input in [choice[0] for choice in choices]:
            for choice in choices:
                if tracker_input == choice[0]:
                    click.secho(f"Using tracker: {choice}", fg="green")
                    return choice
        elif tracker_input.lower().startswith("n"):
            return None


async def choose_tracker_first_time(question="Which tracker would you like to upload to?"):
    """Specific logic for the first time a tracker choice is offered.
    Uses default if there is one and uses the only tracker if there is only one."""
    choices = tracker_list
    if len(choices) == 1:
        click.secho(f"Using tracker: {choices[0]}")
        return choices[0]
    if tracker_cfg.default_tracker:
        click.secho(f"Using tracker: {tracker_cfg.default_tracker}", fg="green")
        return tracker_cfg.default_tracker
    click.secho(question, fg="magenta")
    tracker = await choose_tracker(choices)
    return tracker


async def validate_tracker(ctx, param, value):
    """Only allow trackers in the config tracker dict.
    If it isn't there. Prompt to choose.
    """
    try:
        if value is None:
            return await choose_tracker_first_time()
        if value.upper() in tracker_list:
            click.secho(f"Using tracker: {value.upper()}", fg="green")
            return value.upper()
        else:
            click.secho(f"{value} is not a tracker in your config.", fg="red")
            return await choose_tracker(tracker_list)
    except AttributeError:
        raise click.BadParameter(
            "This flag requires a tracker. Possible sources are: " + ", ".join(tracker_list)
        ) from None


def validate_request(gazelle_site, request):
    """Check the request id is a url or number. and return the number.
    Should it check more? Currently not checking it is the right tracker.
    """
    try:
        if request is None:
            return None
        if request.strip().isdigit():
            pass
        elif request.strip().lower().startswith(gazelle_site.base_url + "/requests.php"):
            request = parse.parse_qs(parse.urlparse(request).query)["id"][0]
        click.secho(
            f"Attempting to fill {gazelle_site.base_url}/requests.php?action=view&id={request}",
            fg="green",
        )
        return request
    except (KeyError, AttributeError):
        raise click.BadParameter("This flag requires a request, either as a url or ID") from None
