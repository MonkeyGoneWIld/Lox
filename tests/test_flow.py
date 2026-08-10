"""Exercise the flow protocol the way the UI drives it."""

import asyncio
import sys

from lox.flow import FlowRegistry

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


async def wait_for_step(flow, timeout: float = 2.0):
    """Block until the flow publishes a question, the way the UI polls."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if flow.step is not None:
            return flow.step
        await asyncio.sleep(0.01)
    raise AssertionError(f"no step appeared: state={flow.state}")


async def main() -> int:
    """Run the checks."""
    registry = FlowRegistry()

    # --- a flow that asks several typed questions ---------------------
    async def driver(flow):
        flow.progress("asking", 0.0)
        proceed = await flow.confirm("Upload to an existing group?", default=False)
        quality = await flow.choose(
            "Is this release lossy mastered?",
            [
                {"value": "no", "label": "No"},
                {"value": "yes", "label": "Yes"},
                {"value": "abort", "label": "Abort", "detail": "Stop here"},
            ],
            default="no",
        )
        picks = await flow.choose_many(
            "Which spectrals?",
            [{"value": str(i), "label": f"Track {i}"} for i in range(1, 4)],
            default=["1"],
        )
        flow.note("assembled")
        flow.progress("uploading", 100.0)
        return {"existing_group": proceed, "lossy": quality, "spectrals": picks}

    flow = registry.start("upload", "Test upload", driver)

    step = await wait_for_step(flow)
    check("first question is published", step.kind == "confirm", step.prompt)
    check("flow reports waiting", flow.state == "waiting", flow.state)
    check("progress visible while waiting", flow.stage == "asking")

    check("stale step id is rejected", flow.answer("nonsense", True) is False)
    check("correct step id is accepted", flow.answer(step.id, True) is True)

    step = await wait_for_step(flow)
    check("choice carries its options", step.kind == "choice" and len(step.options) == 3)
    check("choice carries a default", step.default == "no")
    flow.answer(step.id, "yes")

    step = await wait_for_step(flow)
    check("multi-select is offered", step.kind == "multi" and len(step.options) == 3)
    flow.answer(step.id, ["1", "3"])

    await asyncio.wait_for(flow.task, timeout=2)
    check("flow completes", flow.state == "done", flow.state)
    check("answers reach the driver",
          flow.result == {"existing_group": True, "lossy": "yes", "spectrals": ["1", "3"]},
          str(flow.result))
    check("notes recorded", any(e["message"] == "assembled" for e in flow.events))

    # --- cancelling while it waits ------------------------------------
    async def waiter(flow):
        await flow.confirm("Never answered?")
        return {"reached": True}

    stuck = registry.start("upload", "Cancel me", waiter)
    await wait_for_step(stuck)
    check("cancel while waiting returns True", stuck.cancel() is True)
    await asyncio.sleep(0.05)
    check("cancelled flow stops", stuck.state == "cancelled", stuck.state)
    check("driver did not finish", stuck.result is None)

    # --- a driver that raises -----------------------------------------
    async def broken(flow):
        raise ValueError("kaboom")

    failed = registry.start("check", "Break", broken)
    await asyncio.sleep(0.05)
    check("failure is captured, not swallowed", failed.state == "failed", failed.error or "")
    check("error names the cause", "kaboom" in (failed.error or ""))

    # --- registry bookkeeping -----------------------------------------
    check("active excludes finished flows", registry.active() == [])
    check("summaries cover every flow", len(registry.summaries()) == 3)
    check("filter by kind works", len(registry.summaries("upload")) == 2)
    check("clearing drops finished flows", registry.clear_finished() == 3)

    failed_names = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed_names)}/{len(results)} passed")
    if failed_names:
        print("failed: " + ", ".join(failed_names))
    return 1 if failed_names else 0


sys.exit(asyncio.run(main()))
