"""Source prepended to every routine run, inside phone-harness.

phone-harness execs it with its helpers as globals (find_text, screen, screenshot,
current_app, ...). Only stdlib imports: a routine replays with no LLM and no extra
packages. Events leave as `@@POCKET {json}` lines the engine parses; anything else
the script prints is kept as plain output.
"""

import json
import time

PARAMS = {}
POLL_S = 0.5


class CheckpointMiss(Exception):
    """The expected screen never showed up; the run stops at this checkpoint."""


def _emit(event, **fields):
    print("@@POCKET " + json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def _probe(check):
    """One guarded poll. A helper that raises (screen not ready yet) means 'not seen yet'."""
    try:
        return check()
    except Exception:
        return False


def _seen(query, exact):
    return _probe(lambda: bool(find_text(query, exact=exact)))


def _in_app(app):
    return _probe(lambda: app in str(current_app()))


def _matches(text, any_of, app, exact):
    if text is not None and not _seen(text, exact):
        return False
    if any_of is not None and not any(_seen(t, exact) for t in any_of):
        return False
    return app is None or _in_app(app)


def _expected(text, any_of, app):
    """What the checkpoint wanted, in words, for the failure record."""
    parts = []
    if text is not None:
        parts.append(text)
    if any_of is not None:
        parts.append("any of " + " | ".join(any_of))
    if app is not None:
        parts.append("app " + app)
    return ", ".join(parts)


def _evidence():
    """Screenshot path and visible text for a failure. Never raises: the miss must still be reported."""
    try:
        return screenshot(), [box["text"] for box in screen()][:40]
    except Exception:
        return None, []


def step(name):
    """Mark a phase of the routine; the step log shows these."""
    _emit("step", name=name)


def checkpoint(name, text=None, any_of=None, app=None, timeout=10.0, exact=False):
    """Assert the expected screen, polling until it shows or the timeout passes.

    On a miss it emits what was on screen plus a screenshot, then raises, so the
    engine records the failure and the agent can heal the routine.
    """
    _emit("checkpoint", name=name, status="started")
    deadline = time.time() + timeout
    while not _matches(text, any_of, app, exact):
        if time.time() >= deadline:
            shot, seen = _evidence()
            _emit("checkpoint", name=name, status="failed", expected=_expected(text, any_of, app),
                  seen_text=seen, screenshot=shot)
            raise CheckpointMiss(name)
        time.sleep(POLL_S)
    _emit("checkpoint", name=name, status="passed")


def done(**summary):
    """Report the routine's outcome; the last event of a successful run."""
    _emit("done", **summary)
