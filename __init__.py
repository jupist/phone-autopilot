"""Phone Autopilot: teach a phone task once, replay it without the model.

Companion to the phone-harness plugin. The agent explores with phone_exec, saves what worked
as a routine (approved by the user), and replays it through the `routine` tool or /routines.
"""
from __future__ import annotations

import functools
from pathlib import Path


PLUGIN = "phone-autopilot"
SKILL_MD = Path(__file__).parent / "skills" / PLUGIN / "SKILL.md"

BLUEPRINT_KEY = "daily-routine"
BLUEPRINT_SLOTS = [
    {"name": "routine", "type": "text", "label": "Which routine?", "default": "daily-claim",
     "help": "the name of a routine saved with the routine tool"},
    {"name": "time", "type": "time", "label": "What time?", "default": "09:00"},
    {"name": "recurrence", "type": "weekdays", "label": "Which days?", "default": "everyday",
     "options": ["everyday", "weekdays", "weekends"]},
    {"name": "deliver", "type": "enum", "label": "Where to deliver?", "default": "origin",
     "options": ["origin", "local"], "strict": False},
]
BLUEPRINT_PROMPT = (
    "Run the Phone Autopilot routine '{routine}' with the routine tool (action run). Report the "
    "result in one or two lines. If it fails, do not try to repair it unattended: say which "
    "checkpoint failed, include the failure screenshot path, and tell me to reply 'fix {routine}' "
    "in a chat where I can approve the repair."
)


def _skill_description(path: Path) -> str:
    """The `description:` line of SKILL.md. Hermes lists a plugin skill by what register_skill is given."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    raise ValueError(f"{path} has no description")


def register(ctx) -> None:
    # Imported here, not at module top: pytest imports this file as a bare module while collecting
    # the package, where relative imports have no parent package.
    from . import commands, engine, tool

    ctx.register_tool(
        name="routine", toolset=PLUGIN, schema=tool.SCHEMA, handler=tool.routine,
        check_fn=engine.phone_harness_available, emoji="🤖",
    )
    ctx.register_hook("pre_tool_call", tool.on_pre_tool_call)
    ctx.register_hook("post_tool_call", tool.on_post_tool_call)
    ctx.register_command(
        "routines", handler=functools.partial(commands.handle_routines, set_config=ctx.set_config),
        description="List, run, show or delete phone routines; allow apps they may use",
        args_hint="[list | show <name> | run <name> | delete <name> | apps | allow <app> | revoke <app>]",
    )
    ctx.register_automation_blueprint(
        BLUEPRINT_KEY,
        title="Run a phone routine daily",
        description="Replay a saved phone routine at a set time, with no model in the loop. "
                    "If it breaks, it reports the failed checkpoint and waits for you.",
        category="phone",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=BLUEPRINT_PROMPT,
        slots=BLUEPRINT_SLOTS,
        tags=["phone", "daily"],
    )
    ctx.register_skill(PLUGIN, SKILL_MD, description=_skill_description(SKILL_MD))
