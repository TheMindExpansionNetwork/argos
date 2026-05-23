"""Modal smoke test for ARGOS mock swarm robotics stack.

This clones the GitHub fork inside Modal, installs only core/dev dependencies, runs a
headless mock-hardware smoke, and returns a structured receipt. It intentionally
avoids Unitree hardware, Claude/API calls, Isaac/ROS extras, and GPU spend.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import modal

APP_NAME = "argos-xdrop-modal-smoke"
FORK_URL = "https://github.com/TheMindExpansionNetwork/argos.git"
UPSTREAM_URL = "https://github.com/knoxsbyte/argos.git"
COMMIT = "52cd757324ea9828fac9c66994a7260d11e0743c"

app = modal.App(APP_NAME)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git")
    .pip_install("pytest", "pytest-asyncio")
)


def _run(cmd: str, cwd: str | None = None, timeout: int = 180) -> dict:
    t0 = time.time()
    p = subprocess.run(cmd, cwd=cwd, shell=True, text=True, capture_output=True, timeout=timeout)
    return {
        "cmd": cmd,
        "cwd": cwd,
        "returncode": p.returncode,
        "seconds": round(time.time() - t0, 3),
        "stdout_tail": p.stdout[-4000:],
        "stderr_tail": p.stderr[-4000:],
    }


@app.function(image=image, timeout=900, cpu=2, memory=4096)
def run_argos_smoke() -> dict:
    steps: list[dict] = []
    work = Path("/tmp/argos")
    steps.append(_run(f"git clone --depth 1 {FORK_URL} {work}", timeout=180))
    steps.append(_run(f"git fetch --depth 1 origin {COMMIT}", cwd=str(work), timeout=120))
    steps.append(_run(f"git checkout {COMMIT}", cwd=str(work), timeout=120))
    actual_commit = subprocess.check_output("git rev-parse HEAD", cwd=work, shell=True, text=True).strip()

    # Install core package only. Do not use .[dev], because optional IsaacLab/omniverse
    # extras are heavyweight and not needed for the headless mock path.
    steps.append(_run("python -m pip install -e .", cwd=str(work), timeout=300))
    steps.append(_run("python -m compileall -q argos tests", cwd=str(work), timeout=120))
    steps.append(_run("python -m pytest -q tests/test_navigation.py tests/test_tasks.py tests/test_swarm.py", cwd=str(work), timeout=300))

    smoke_code = r'''
import asyncio, json
from argos.comm.unitree_bridge import MockUnitreeBridge, G1Config
from argos.navigation.zones import ZoneManager
from argos.navigation.coverage import BoustrophedonPlanner
from argos.swarm.dependency import TaskDAG, TaskNode
from argos.swarm.allocator import AuctionAllocator
from argos.tasks.solo import SweepFloorTask

async def main():
    robots = [MockUnitreeBridge(G1Config(ip="10.10.0.1", name="Modal-G1-A")), MockUnitreeBridge(G1Config(ip="10.10.0.2", name="Modal-G1-B"))]
    for r in robots:
        await r.connect()
    mgr = ZoneManager(room_bounds=(0, 0, 6, 4))
    zones = mgr.partition(num_robots=2, strategy="strips")
    planner = BoustrophedonPlanner(step_size=0.75)
    waypoints = [planner.plan(z, start_pos=(0,0)) for z in zones]
    dag = TaskDAG()
    dag.add_task(TaskNode("clean-zone-a", "sweep_floor", {"zone": zones[0].zone_id}, min_robots=1))
    dag.add_task(TaskNode("clean-zone-b", "sweep_floor", {"zone": zones[1].zone_id}, min_robots=1))
    allocator = AuctionAllocator(robots)
    states = {r.robot_id: await r.get_state() for r in robots}
    assignments = allocator.assign(dag, states)
    task = SweepFloorTask("modal-sweep", {"zone_bounds": zones[0].bounds})
    result = await task.execute([robots[0]])
    for r in robots:
        await r.disconnect()
    print(json.dumps({
        "robots": [r.robot_id for r in robots],
        "zone_count": len(zones),
        "waypoint_counts": [len(w) for w in waypoints],
        "assignment_robot_count": len(assignments),
        "assigned_task_count": sum(len(v) for v in assignments.values()),
        "task_success": bool(result.success),
        "task_duration_seconds": result.duration_seconds,
    }, sort_keys=True))
asyncio.run(main())
'''
    smoke_path = work / "modal_headless_smoke.py"
    smoke_path.write_text(smoke_code)
    smoke_step = _run("python modal_headless_smoke.py", cwd=str(work), timeout=180)
    steps.append(smoke_step)

    ok = all(s["returncode"] == 0 for s in steps)
    smoke_json = None
    if smoke_step["returncode"] == 0:
        try:
            smoke_json = json.loads(smoke_step["stdout_tail"].strip().splitlines()[-1])
        except Exception as e:
            smoke_json = {"parse_error": repr(e), "raw": smoke_step["stdout_tail"][-1000:]}

    return {
        "ok": ok,
        "app": APP_NAME,
        "fork_url": FORK_URL,
        "upstream_url": UPSTREAM_URL,
        "commit": actual_commit,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "modal_lane": "CPU headless mock robotics swarm smoke",
        "smoke": smoke_json,
        "steps": steps,
    }


@app.local_entrypoint()
def main(out_dir: str = "/opt/data/workspace/x-dropped-projects/argos-modal-receipt"):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = run_argos_smoke.remote()
    (out / "receipt.json").write_text(json.dumps(result, indent=2, sort_keys=True))
    (out / "README_MODAL_RECEIPT.md").write_text(
        "# ARGOS X-drop Modal smoke receipt\n\n"
        f"- App: `{APP_NAME}`\n"
        f"- Fork: {FORK_URL}\n"
        f"- Upstream: {UPSTREAM_URL}\n"
        f"- Commit: `{result.get('commit')}`\n"
        f"- OK: `{result.get('ok')}`\n"
        f"- Lane: `{result.get('modal_lane')}`\n\n"
        "## Smoke JSON\n\n```json\n"
        + json.dumps(result.get("smoke"), indent=2, sort_keys=True)
        + "\n```\n\n"
        "## Notes\n\n"
        "This proves ARGOS can be built and exercised on Modal without robot hardware, GPU, Claude API, ROS, IsaacLab, or Unitree SDK. "
        "The Modal function clones the GitHub fork, installs the core Python package, compiles the source, runs focused tests, and executes a mock two-robot zone/task allocation smoke.\n"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
