"""Ask a yes/no question and actually wait for the answer.

``asyncclick.confirm`` is synchronous -- it blocks on a terminal read -- while
``asyncclick.prompt`` is a coroutine. The pipeline was written against that and
calls ``click.confirm(...)`` bare, which is correct for a terminal and wrong for
anything that answers questions over a network: the web UI replaces confirm with
something that has to be awaited before a person has seen the question, let
alone answered it.

An un-awaited replacement still has to return *something*, and whatever it
returns is used as the answer. That silently answered every question in the
upload with its default -- renaming files, sanitising a failed integrity check
and posting the torrent all decided themselves, and the questions whose default
is "no" turned into aborts with nothing on screen to explain them.

So call sites in async code use this instead. It awaits when the confirm hands
back something awaitable and passes the value straight through when it does not,
which keeps the terminal path working exactly as before.
"""

import inspect
from typing import Any

import asyncclick as click

__all__ = ["confirm", "edit"]


async def confirm(text: str, **kwargs: Any) -> bool:
    """Ask a yes/no question, awaiting the answer if the answer is awaitable.

    Args:
        text: The question, already styled.
        **kwargs: Passed through to ``click.confirm`` -- ``default``, ``abort``.

    Returns:
        What the user answered.

    Raises:
        click.Abort: If ``abort=True`` was passed and the answer was no.
    """
    # Annotated Any deliberately: click types confirm as returning bool, so
    # without this the awaitable branch narrows to Never and the await below
    # becomes a type error -- when the awaitable case is the whole point.
    result: Any = click.confirm(text, **kwargs)
    if inspect.isawaitable(result):
        return bool(await result)
    return bool(result)


async def edit(text: str = "", **kwargs: Any) -> str | None:
    """Edit a block of text, awaiting the result if it is awaitable.

    ``click.edit`` shells out to ``$EDITOR``. The web UI replaces it with a form
    and so has to be awaited; a terminal returns the edited string directly.

    Args:
        text: What to edit.
        **kwargs: Passed through to ``click.edit`` -- ``editor``, ``extension``.

    Returns:
        The edited text, or None if the edit was cancelled.
    """
    result: Any = click.edit(text, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return None if result is None else str(result)
