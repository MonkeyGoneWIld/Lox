"""Check the CLI-prompt to UI-control bridge against real pipeline output."""

import asyncio
import sys

from lox.flow import Flow
from lox.upload_flow import FlowPrompts, default_letter, parse_options, strip_ansi

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


async def wait_for_step(flow, timeout: float = 2.0):
    """Block until a question appears."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if flow.step is not None:
            return flow.step
        await asyncio.sleep(0.01)
    raise AssertionError("no step appeared")


async def main() -> int:
    """Run the checks."""
    # --- bracket prompts become buttons -------------------------------
    lossy = strip_ansi(
        "\x1b[35m\nIs this release lossy mastered? "
        "[y]es, [N]o, [r]eopen spectrals, [a]bort, [d]elete music folder\x1b[0m"
    )
    options = parse_options(lossy)
    check("lossy prompt yields five options", len(options) == 5, str([o["value"] for o in options]))
    check("destructive answers marked", [o["value"] for o in options if o["danger"]] == ["a", "d"])
    check("capitalised initial is the default", default_letter(lossy) == "n")

    # --- duplicate groups printed before a prompt become options ------
    flow = Flow("upload", "test")
    prompts = FlowPrompts(flow)

    async def driver():
        # Exactly what the dupe checker emits, including the split write.
        prompts._echo("Results matching this release were found on OPS:")
        prompts._echo(" 01 >> 1605624 | ", nl=False)
        prompts._echo("Charles Ryan - Jiggy Buckaroo (2025) [Album] [Tags: rap] | "
                      "https://orpheus.network/torrents.php?id=1605624")
        return await prompts._prompt(
            "Would you like to upload to an existing group? Paste a URL, pick from groups found or "
            "[N]ew group / [a]bort / [d]elete music folder"
        )

    task = asyncio.create_task(driver())
    step = await wait_for_step(flow)

    values = [o["value"] for o in step.options]
    check("found group offered as an option", any("1605624" in str(v) for v in values), str(values[:1]))
    check("group option is the url", values[0].startswith("https://orpheus.network"), values[0])
    check("group label is readable",
          "Jiggy Buckaroo" in step.options[0]["label"], step.options[0]["label"])
    check("bracket options still present", {"n", "a", "d"} <= set(values[1:]), str(values[1:]))
    check("split writes joined into one note",
          any("1605624" in e["message"] and "orpheus" in e["message"] for e in flow.events))

    flow.answer(step.id, values[0])
    answer = await asyncio.wait_for(task, timeout=2)
    check("chosen url returned to the pipeline", answer == values[0])

    # --- groups do not leak into the next, unrelated question ---------
    flow2 = Flow("upload", "test2")
    prompts2 = FlowPrompts(flow2)
    task2 = asyncio.create_task(prompts2._prompt("Any comment for the report?"))
    step2 = await wait_for_step(flow2)
    check("plain prompt stays a text field", step2.kind == "text", step2.kind)
    flow2.answer(step2.id, "none")
    await asyncio.wait_for(task2, timeout=2)

    # --- confirm maps to a yes/no ------------------------------------
    flow3 = Flow("upload", "test3")
    prompts3 = FlowPrompts(flow3)
    task3 = asyncio.create_task(prompts3._confirm("Would you like to rename the files?", default=True))
    step3 = await wait_for_step(flow3)
    check("confirm becomes a confirm step", step3.kind == "confirm" and step3.default is True)
    flow3.answer(step3.id, False)
    check("confirm returns the answer", await asyncio.wait_for(task3, timeout=2) is False)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
