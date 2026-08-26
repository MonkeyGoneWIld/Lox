# Interface prototype

Design source for the Lox redesign. Nothing here is wired into the app yet —
these are the drawings, kept in the repo so the decision and its reasoning
live next to the code they describe.

Direction A, "Console", was chosen. `Main.dc.html` is a clickable prototype
of the pipeline; the other two directions are kept on a second page for
reference.

| file | what it is |
|---|---|
| `Main.dc.html` | the prototype — rail, queue, downloads, the upload flow, requests, settings |
| `Flow.dc.html` | why the navigation changed |
| `DirectionB.dc.html` | not chosen — Editorial |
| `DirectionC.dc.html` | not chosen — Studio |
| `canvas.json` | layout, pages and the notes |
| `check-prototype.mjs` | drives every handler in the prototype |

## Running the checks

    node design/check-prototype.mjs

The artboard renders inside a sandboxed iframe, so nothing outside it can
click its buttons. The logic class is plain JS, so this lifts it out and
drives it: every handler is called and the state it produces is checked,
including that no value handed to the template is undefined — an undefined
one interpolates the word "undefined" into a CSS declaration and silently
drops it.

It says the flow works. It cannot say the flow looks right.

## The two problems this answers

**Looks like every AI-built app.** The palette was `--accent: #a238ff` on
`#16161d` with `system-ui` and 10px radii, which is the house style of
nearly every AI-generated app. Replaced with a warm near-black, one amber
that only ever means "this needs you", green only for "there is something
to upload here", IBM Plex Sans with Plex Mono for anything compared across
rows. No violet, no pills, no gradients.

**Makes very little sense.** Eight peer tabs, in an order nobody chose, for
what is really four ways of finding work and a three-stage pipeline. Now
two groups: the four entry points renamed for what they do, and a numbered
pipeline whose counts are visible from every screen — including how many
runs are sitting blocked waiting for an answer.
