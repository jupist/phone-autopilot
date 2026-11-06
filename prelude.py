"""Source prepended to every routine run, inside phone-harness.

phone-harness execs it with its helpers as globals (find_text, screen, screenshot,
current_app, ...). Only stdlib imports at the top: a routine replays with no LLM and no extra
packages. Image checks import Pillow lazily (Hermes ships it), so text-only routines never need
it. Events leave as `@@POCKET {json}` lines the engine parses; anything else
the script prints is kept as plain output.
"""

import json
import time

PARAMS = {}
REFS = {}
POLL_S = 0.5
MATCH = 0.92
_PIXEL_TOLERANCE = 32


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


def _live_image():
    from PIL import Image

    path, _bounds = send("screen.capture")
    with Image.open(path) as img:
        return img.convert("RGB")


def _similarity(a, b):
    """Share of pixels that match, 1.0 for identical crops.

    A pixel counts as different only past a per-pixel tolerance, so compression and
    anti-aliasing noise never fail a check, while a different screen or tile (even one of
    similar brightness) scores near zero instead of averaging out.
    """
    from PIL import ImageChops, ImageStat

    a = a.convert("RGB").resize((48, 48))
    b = b.convert("RGB").resize((48, 48))
    diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > _PIXEL_TOLERANCE else 0)
    return 1.0 - ImageStat.Stat(diff).mean[0] / 255.0


def _crop(img, region):
    x, y, w, h = region
    return img.crop((x, y, x + w, y + h))


def looks_like(ref, threshold=MATCH):
    """Similarity of the live screen to a saved ref, and whether it clears the threshold."""
    from PIL import Image

    spec = REFS[ref]
    with Image.open(spec["path"]) as saved:
        score = _similarity(_crop(_live_image(), spec["region"]), saved)
    return score >= threshold, round(score, 3)


def snap(region):
    """A crop of the live screen, to compare later with changed()."""
    return _crop(_live_image(), region)


def changed(before, region, min_diff=0.02):
    """True when a region no longer matches an earlier snap(): a move landed, a counter ticked."""
    return 1.0 - _similarity(_crop(_live_image(), region), before) >= min_diff


def _image_ok(image, threshold):
    return _probe(lambda: looks_like(image, threshold)[0])


def _matches(text, any_of, app, exact, image=None, threshold=MATCH, changed_from=None):
    if changed_from is not None and not _probe(lambda: changed(*changed_from)):
        return False
    if image is not None and not _image_ok(image, threshold):
        return False
    if text is not None and not _seen(text, exact):
        return False
    if any_of is not None and not any(_seen(t, exact) for t in any_of):
        return False
    return app is None or _in_app(app)


def _expected(text, any_of, app, image=None, changed_from=None):
    """What the checkpoint wanted, in words, for the failure record."""
    parts = [f"region {list(changed_from[1])} changed"] if changed_from is not None else []
    if image is not None:
        parts.append(f"screen matching ref {image!r}")
    if text is not None:
        parts.append(text)
    if any_of is not None:
        parts.append("any of " + " | ".join(any_of))
    if app is not None:
        parts.append("app " + app)
    return ", ".join(parts)


def _evidence():
    """Screenshot path and visible text for a failure. Never raises: the miss must still be reported.

    Each half is captured on its own: animated screens (a ticking clock, video) never let the
    accessibility tree settle, and the screenshot is the evidence a repair needs most then.
    """
    try:
        shot = screenshot()
    except Exception:
        shot = None
    try:
        seen = [box["text"] for box in screen()][:40]
    except Exception as exc:
        seen = [f"(screen text unavailable: {type(exc).__name__}; use the screenshot)"]
    return shot, seen


def step(name):
    """Mark a phase of the routine; the step log shows these."""
    _emit("step", name=name)


def checkpoint(name, text=None, any_of=None, app=None, timeout=10.0, exact=False, image=None,
               threshold=MATCH, changed_from=None):
    """Assert the expected screen, polling until it shows or the timeout passes.

    changed_from=(before, region) passes once that region differs from an earlier snap(), so
    "the move landed" fails like any other checkpoint, with a screenshot the agent can heal from.

    On a miss it emits what was on screen plus a screenshot, then raises, so the
    engine records the failure and the agent can heal the routine.
    """
    _emit("checkpoint", name=name, status="started")
    deadline = time.time() + timeout
    while not _matches(text, any_of, app, exact, image, threshold, changed_from):
        if time.time() >= deadline:
            shot, seen = _evidence()
            extra = {"similarity": _probe(lambda: looks_like(image, threshold)[1])} if image else {}
            _emit("checkpoint", name=name, status="failed", expected=_expected(text, any_of, app, image, changed_from),
                  seen_text=seen, screenshot=shot, **extra)
            raise CheckpointMiss(name)
        time.sleep(POLL_S)
    _emit("checkpoint", name=name, status="passed")


def done(**summary):
    """Report the routine's outcome; the last event of a successful run."""
    _emit("done", **summary)
