# Interface directions

Design source for a proposed redesign of the Lox web UI. Nothing here is
wired into the app — these are the drawings, kept in the repo so the
decision and its reasoning live with the code.

Each `.dc.html` is one artboard on a canvas:

| file | what it argues |
|---|---|
| `Flow.dc.html` | the navigation problem: eight peer tabs for four entry points and a three-stage pipeline |
| `Main.dc.html` | direction A, Console — the leading candidate, at full size |
| `DirectionB.dc.html` | direction B, Editorial — paper-light, serif |
| `DirectionC.dc.html` | direction C, Studio — the smallest leap from today |
| `canvas.json` | layout and the notes making the case for and against each |

## Why

The current palette is `--accent: #a238ff` on `#16161d`, `system-ui`, 10px
radii. That combination is the house style of nearly every AI-generated
app, which is what makes it read as one. The three directions each replace
it with a deliberate one.

The flow problem is separate and is not a matter of taste, so all three
directions fix it the same way: the four ways of finding work are grouped
and renamed for what they do, and Found/Downloads/Uploads become a
numbered pipeline whose counts are visible from every screen — including
how many runs are blocked waiting for an answer.
