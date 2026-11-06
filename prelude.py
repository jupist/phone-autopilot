"""Source prepended to every routine run, inside phone-harness.

phone-harness execs it with its helpers as globals (find_text, screen, screenshot,
current_app, ...). Only stdlib imports at the top: a routine replays with no LLM and no extra
packages. Image checks import Pillow lazily (Hermes ships it), so text-only routines never need
it. Events leave as `@@POCKET {json}` lines the engine parses; anything else
the script prints is kept as plain output.
"""

import atexit
import json
import re
import sys
import threading
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


class _NoText(Exception):
    """The screen never settles (a clock, video, a playing track), so Android will not read it."""


def _texts():
    """Visible text, read once per poll: a read costs about 2.5s, so it is never repeated per word."""
    try:
        return [box["text"] for box in screen()]
    except Exception as exc:
        # Each refusal costs ~10s inside uiautomator and the next read refuses again.
        if type(exc).__name__ == "TreeUnavailable":
            raise _NoText() from exc
        raise


def _has(texts, query, exact):
    q = query.lower()
    return any((t.lower() == q) if exact else (q in t.lower()) for t in texts)


def _in_app(app):
    return _probe(lambda: app in str(current_app()))


def open_app(name, timeout=10.0):
    """Launch an app and return its package once it is in front.

    Replaces phone-harness's open_app, which then waits for the screen to stop changing: on a
    clock, a video or a playing track that never happens, and every launch cost its 6s timeout.
    The checkpoint after a launch is what waits for the right screen.
    """
    pkg = send("apps.launch", name=name) or name
    deadline = time.time() + timeout
    while time.time() < deadline and not _in_app(pkg):
        time.sleep(0.1)
    return pkg


def _capture():
    """The live screen and the device rectangle it shows (None when the transport gives none)."""
    from PIL import Image

    path, bounds = send("screen.capture")
    with Image.open(path) as img:
        return img.convert("RGB"), bounds


def _live_image():
    return _capture()[0]


# Image comparison. Sized so text stays legible (a 48x48 thumbnail made any two labels look alike);
# a pixel differs only when nothing within a pixel of it in the other image is close in colour, and
# the images may sit a couple of pixels apart, so layout jitter is not a change; and the score is
# taken against the "ink" (pixels that stand out from the background), so one different word on a
# plain panel counts. Colour, not brightness: a green toggle and a grey one can be equally bright.
_FIT_SIDE = 320
_ALIGN_PX = 2
_INK_FLOOR = 0.05
# Some apps draw with a fine dot pattern (and shift it to protect OLED screens), so two shots of the
# same text differ pixel by pixel. A whole-number downscale plus a light blur averages that out
# while strokes stay legible.
_SMOOTH = 1.0


def _fit(img, size=None):
    from PIL import Image, ImageFilter

    img = img.convert("RGB")
    if size is None:
        factor = -(-max(img.size) // _FIT_SIDE)
        size = (max(1, img.width // factor), max(1, img.height // factor))
    return img.resize(size, Image.BOX).filter(ImageFilter.GaussianBlur(_SMOOTH))


def _over(img):
    """Pixels where any colour channel exceeds the tolerance, as a 0/255 mask."""
    from PIL import ImageChops

    r, g, b = img.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v > _PIXEL_TOLERANCE else 0)


def _count(mask):
    from PIL import ImageStat

    return ImageStat.Stat(mask).sum[0] / 255


def _ink(img):
    from PIL import Image, ImageChops

    pixels = sorted(img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata(),
                    key=sum)
    background = Image.new("RGB", img.size, pixels[len(pixels) // 2])
    return _over(ImageChops.difference(img, background))


def _envelope(img):
    """The darkest and brightest colour in each pixel's 3x3 neighbourhood."""
    from PIL import ImageFilter

    return img.filter(ImageFilter.MinFilter(3)), img.filter(ImageFilter.MaxFilter(3))


def _outside(a, envelope):
    """a's pixels with no close colour anywhere in the other image's 3x3 neighbourhood."""
    from PIL import ImageChops

    low, high = envelope
    return _over(ImageChops.lighter(ImageChops.subtract(low, a), ImageChops.subtract(a, high)))


def _similarity(a, b):
    """1.0 for the same content, low for different content, even when only a word differs."""
    from PIL import ImageChops

    a = _fit(a)
    b = _fit(b, a.size)
    ink = _count(ImageChops.lighter(_ink(a), _ink(b)))
    # Neighbourhood filters are the slow part, so each image is filtered once and the filtered
    # result moves with it, instead of filtering again at every alignment offset.
    a_env, b_env = _envelope(a), _envelope(b)
    differing = min(
        _count(ImageChops.lighter(
            _outside(a, tuple(ImageChops.offset(e, dx, dy) for e in b_env)),
            _outside(ImageChops.offset(b, dx, dy), a_env)))
        for dx in range(-_ALIGN_PX, _ALIGN_PX + 1) for dy in range(-_ALIGN_PX, _ALIGN_PX + 1))
    return max(0.0, 1.0 - differing / max(ink, a.width * a.height * _INK_FLOOR))


# System state that the screen may not show. Read through adb, so it costs ~0.1s on any screen.

_NOTE_FIELD = re.compile(r"^\s*android\.(title|text|subText)=\w+ \((.*)\)\s*$")


def _media_sessions():
    sessions, current = [], None
    for line in str(shell("dumpsys media_session")).splitlines():
        line = line.strip()
        if line.startswith("package="):
            current = {"app": line[len("package="):], "state": "", "description": ""}
            sessions.append(current)
        elif current is not None and line.startswith("state=PlaybackState {state="):
            current["state"] = line.split("{state=", 1)[1].split("(", 1)[0]
        elif current is not None and line.startswith("metadata:") and "description=" in line:
            current["description"] = line.split("description=", 1)[1]
    return [s for s in sessions if s["state"]]


def _media_notification(app):
    """title / text / subText of the app's own playback notification. Other apps' notifications
    pass through this process unread and are never returned."""
    fields, inside = {}, False
    for line in str(shell("dumpsys notification --noredact")).splitlines():
        if "NotificationRecord(" in line:
            if fields:
                break
            inside = f"pkg={app} " in line
        elif inside:
            match = _NOTE_FIELD.match(line)
            if match:
                fields.setdefault(match.group(1), match.group(2))
    return fields


def media_state(app=None):
    """What a media app is playing, read from the system: works on any screen, without opening it.

    {"app", "state", "playing", "title", "artist", "source"}, where source is the playlist, album
    or station the app says it is playing from ("" when it does not say). With no app, the session
    that is playing, else the first one. None when nothing has a media session.
    """
    sessions = _media_sessions()
    if app is not None:
        sessions = [s for s in sessions if s["app"] == app]
    if not sessions:
        return None
    session = next((s for s in sessions if s["state"] == "PLAYING"), sessions[0])
    note = _media_notification(session["app"])
    described = [part.strip() for part in session["description"].split(",")]
    return {
        "app": session["app"],
        "state": session["state"],
        "playing": session["state"] == "PLAYING",
        "title": note.get("title") or (described[0] if described else ""),
        "artist": note.get("text") or (described[1] if len(described) > 1 else ""),
        "source": note.get("subText") or "",
    }


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


# Image search: where a saved ref is on screen now, wherever the layout put it. Pillow only (the
# routine environment has no numpy): a coarse pass on a downscaled screen keeps a few candidates,
# then each is refined at full size and scored with _similarity.
_SEARCH_MAX_SIDE = 220
_SEARCH_KEEP = 4


def _shrink(img, f):
    from PIL import Image

    return img if f == 1 else img.resize((max(1, img.width // f), max(1, img.height // f)), Image.BOX)


def _coarse_hits(screen, ref, f):
    from PIL import ImageChops, ImageStat

    small, tpl = _shrink(screen, f), _shrink(ref, f)
    hits = []
    for y in range(small.height - tpl.height + 1):
        for x in range(small.width - tpl.width + 1):
            box = (x, y, x + tpl.width, y + tpl.height)
            hits.append((ImageStat.Stat(ImageChops.difference(small.crop(box), tpl)).sum[0], x * f, y * f))
    return sorted(hits)[:_SEARCH_KEEP]


def _refine(pyramid, x0, y0, f):
    """Narrow a coarse hit at scale f down to the exact pixel, a few scales at a time.

    Each pass searches ±f around the last hit at a finer scale, so the full-resolution pass only
    looks a few pixels either side instead of across a whole coarse cell.
    """
    from PIL import ImageChops, ImageStat

    while True:
        g = max(1, f // 4)
        screen, ref = pyramid(g)
        best = None
        for y in range(max(0, (y0 - f) // g), min(screen.height - ref.height, (y0 + f) // g) + 1):
            for x in range(max(0, (x0 - f) // g), min(screen.width - ref.width, (x0 + f) // g) + 1):
                box = (x, y, x + ref.width, y + ref.height)
                score = ImageStat.Stat(ImageChops.difference(screen.crop(box), ref)).sum[0]
                if best is None or score < best[0]:
                    best = (score, x * g, y * g)
        if best is None or g == 1:
            return best
        _, x0, y0 = best
        f = g


def _locate(screen, ref):
    """Top-left of the best match of ref in screen and its similarity, or None if it cannot fit."""
    if ref.width > screen.width or ref.height > screen.height:
        return None
    gray, tpl = screen.convert("L"), ref.convert("L")
    f = max(1, min(ref.width, ref.height) // 12, -(-max(screen.size) // _SEARCH_MAX_SIDE))
    scales = {}

    def pyramid(g):
        if g not in scales:
            scales[g] = (_shrink(gray, g), _shrink(tpl, g))
        return scales[g]

    found = []
    for _, x, y in _coarse_hits(gray, tpl, f):
        refined = _refine(pyramid, x, y, f)
        if refined is not None:
            raw, rx, ry = refined
            box = (rx, ry, rx + ref.width, ry + ref.height)
            found.append((_similarity(screen.crop(box), ref), -raw, rx, ry))
    if not found:
        return None
    score, _, x, y = max(found)  # equal scores: the exact pixel match wins
    return score, x, y


def _to_device(x, y, img, bounds):
    """Screenshot pixels to the coordinates tap() takes."""
    if not bounds:
        return x, y
    return (bounds["x"] + x * bounds["w"] / img.width, bounds["y"] + y * bounds["h"] / img.height)


def find_image(ref, threshold=MATCH):
    """Where a saved ref shows on the live screen now, as the (x, y) to tap at its centre, or None.

    Unlike checkpoint(image=...), which checks the region the ref was cut from, this searches the
    whole screen, so a tile, icon or button is found wherever the layout or orientation moved it.
    """
    from PIL import Image

    screen, bounds = _capture()
    with Image.open(REFS[ref]["path"]) as saved:
        ref_img = saved.convert("RGB")
    hit = _locate(screen, ref_img)
    if hit is None or hit[0] < threshold:
        return None
    _, x, y = hit
    return _to_device(x + ref_img.width / 2, y + ref_img.height / 2, screen, bounds)


def tap_image(ref, threshold=MATCH, timeout=10.0):
    """Find a saved ref anywhere on screen and tap it: the way to press things that have no text.

    Waits up to `timeout` for it to show. A miss fails like a checkpoint, with a screenshot.
    """
    name = f"tap {ref}"
    _emit("checkpoint", name=name, status="started")
    deadline = time.time() + timeout
    while True:
        spot = _probe(lambda: find_image(ref, threshold))
        if spot:
            break
        if time.time() >= deadline:
            _miss(name, f"{ref!r} anywhere on screen")
        time.sleep(POLL_S)
    _emit("checkpoint", name=name, status="passed")
    tap(round(spot[0]), round(spot[1]))
    return spot


def _image_ok(image, threshold):
    return _probe(lambda: looks_like(image, threshold)[0])


def _matches(text, any_of, app, exact, image=None, threshold=MATCH, changed_from=None, check=None):
    """Cheapest condition first: the foreground app (~0.06s), a system check, images, then one text read."""
    if app is not None and not _in_app(app):
        return False
    if check is not None and not _probe(check):
        return False
    if changed_from is not None and not _probe(lambda: changed(*changed_from)):
        return False
    if image is not None and not _image_ok(image, threshold):
        return False
    if text is None and any_of is None:
        return True
    try:
        texts = _texts()
    except _NoText:
        raise
    except Exception:  # mid-transition: not seen yet
        return False
    if text is not None and not _has(texts, text, exact):
        return False
    return any_of is None or any(_has(texts, t, exact) for t in any_of)


def _check_name(check):
    name = getattr(check, "__name__", "")
    return f"{name}() to be true" if name and name != "<lambda>" else "the check= condition to be true"


def _expected(text, any_of, app, image=None, changed_from=None, check=None):
    """What the checkpoint wanted, in words, for the failure record."""
    parts = [f"region {list(changed_from[1])} changed"] if changed_from is not None else []
    if check is not None:
        parts.append(_check_name(check))
    if image is not None:
        parts.append(f"screen matching ref {image!r}")
    if text is not None:
        parts.append(text)
    if any_of is not None:
        parts.append("any of " + " | ".join(any_of))
    if app is not None:
        parts.append("app " + app)
    return ", ".join(parts)


def _evidence(read_text=True):
    """Screenshot path and visible text for a failure. Never raises: the miss must still be reported.

    Each half is captured on its own: animated screens (a ticking clock, video) never let the
    accessibility tree settle, and the screenshot is the evidence a repair needs most then.
    """
    try:
        shot = screenshot()
    except Exception:
        shot = None
    if not read_text:  # known unreadable: another read would only burn uiautomator's ~10s wait
        return shot, ["(screen text unavailable: TreeUnavailable; use the screenshot)"]
    try:
        seen = [box["text"] for box in screen()][:40]
    except Exception as exc:
        seen = [f"(screen text unavailable: {type(exc).__name__}; use the screenshot)"]
    return shot, seen


def _miss(name, expected, read_text=True, **extra):
    """Report a failed checkpoint with its evidence and stop the routine there."""
    shot, seen = _evidence(read_text)
    _emit("checkpoint", name=name, status="failed", expected=expected, seen_text=seen, screenshot=shot, **extra)
    raise CheckpointMiss(name)


_ROUTINE_FIRST_LINE = None  # the engine sets this: the script line where the routine body starts


def _failing_line(exc):
    """The routine line that was running when exc was raised: (number, its text) or (None, "").

    The routine body follows the guard and this prelude in one script, so its frames are the ones
    at or after _ROUTINE_FIRST_LINE; the last such frame is the line the routine was on.
    """
    if _ROUTINE_FIRST_LINE is None:
        return None, ""
    if isinstance(exc, SyntaxError):
        script_line = exc.lineno
    else:
        script_line, tb = None, exc.__traceback__
        while tb is not None:
            if tb.tb_frame.f_globals is globals() and tb.tb_lineno >= _ROUTINE_FIRST_LINE:
                script_line = tb.tb_lineno
            tb = tb.tb_next
    if script_line is None or script_line < _ROUTINE_FIRST_LINE:
        return None, ""
    number = script_line - _ROUTINE_FIRST_LINE + 1
    try:
        with open(ROUTINE_DIR + "/routine.py", encoding="utf-8") as source:
            lines = source.read().splitlines()
    except (NameError, OSError):
        lines = []
    return number, lines[number - 1].strip() if number <= len(lines) else ""


def _record_uncaught(kind, exc, tb):
    """Any error that ends the routine leaves the same record as a missed checkpoint.

    A helper that raises (no such text to tap, an app that would not launch, a bug in the routine)
    is a failure a repair has to understand, so it gets the screenshot and screen text a checkpoint
    miss gets, named after the routine line that raised. Python then prints the traceback as usual.
    """
    if not issubclass(kind, (CheckpointMiss, KeyboardInterrupt, SystemExit)):
        number, text = _failing_line(exc)
        name = f"line {number}: {text[:80]}".rstrip(": ") if number else "routine"
        raised = f"{kind.__name__}: {exc}"[:300]
        shot, seen = _evidence()
        _emit("checkpoint", name=name, status="failed", expected=f"this line to run, but it raised {raised}",
              seen_text=seen, screenshot=shot, raised=raised)
    sys.__excepthook__(kind, exc, tb)


def step(name):
    """Mark a phase of the routine; the step log shows these."""
    _emit("step", name=name)


def checkpoint(name, text=None, any_of=None, app=None, timeout=10.0, exact=False, image=None,
               threshold=MATCH, changed_from=None, check=None):
    """Assert the expected screen, polling until it shows or the timeout passes.

    check=callable passes once it returns something truthy: any state the screen cannot show, read
    from the system (`check=dnd_on`, `check=lambda: media_state()["playing"]`). It fails like any
    other checkpoint, with a screenshot, so never use a bare assert for these.

    changed_from=(before, region) passes once that region differs from an earlier snap(), so
    "the move landed" fails like any other checkpoint, with a screenshot the agent can heal from.

    On a miss it emits what was on screen plus a screenshot, then raises, so the
    engine records the failure and the agent can heal the routine.
    """
    _emit("checkpoint", name=name, status="started")
    deadline = time.time() + timeout
    while True:
        try:
            if _matches(text, any_of, app, exact, image, threshold, changed_from, check):
                break
        except _NoText:
            _miss(name, _expected(text, any_of, app, image, changed_from, check)
                  + " (this screen never settles, so it has no readable text: check it with an image ref)",
                  read_text=False)
        if time.time() >= deadline:
            extra = {"similarity": _probe(lambda: looks_like(image, threshold)[1])} if image else {}
            _miss(name, _expected(text, any_of, app, image, changed_from, check), **extra)
        time.sleep(POLL_S)
    _emit("checkpoint", name=name, status="passed")


def done(**summary):
    """Report the routine's outcome; the last event of a successful run."""
    _emit("done", **summary)


# Where the time goes. Every helper a routine calls is timed, counting only the outermost call (the
# screen reads inside a checkpoint are the checkpoint's time), and the totals leave as one "cost"
# event when the script ends, pass or fail. That is how a slow routine shows its cause instead of
# just a long total. Helpers that answer at once carry a budget: a call far over it means that
# helper or the phone link is slow, and the run names it. Helpers that wait (a checkpoint, a launch)
# have none, since their time is the app's. A budget marks "something is wrong", not a typical
# call: a text read is ~2.5s and up to ~4s on a busy screen, while a screen that never settles
# takes ~12s.
_BUDGET_S = {
    "screen": 6.0, "find_text": 6.0, "tap_text": 6.0, "tap_ui": 6.0, "screenshot": 1.5,
    "shell": 1.0, "current_app": 0.5, "tap": 0.5, "long_press": 1.5, "swipe": 1.5, "press": 0.5,
    "type_text": 3.0, "home": 0.5, "back": 0.5, "media_state": 1.0, "find_image": 1.5,
    "looks_like": 1.5, "snap": 1.5, "changed": 1.5,
}
_WAITS = ("checkpoint", "open_app", "tap_image", "wait", "wait_for_text", "wait_for_app", "scroll_until")
_COST = {}
_DEPTH = [0]


def _timed(name, fn):
    def timed(*args, **kwargs):
        if threading.current_thread() is not threading.main_thread():
            return fn(*args, **kwargs)  # the app guard's polling is not the routine's time
        _DEPTH[0] += 1
        start = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            _DEPTH[0] -= 1
            if _DEPTH[0] == 0:
                spent = time.monotonic() - start
                entry = _COST.setdefault(name, [0, 0.0, 0.0])
                entry[0] += 1
                entry[1] += spent
                entry[2] = max(entry[2], spent)

    timed.__name__, timed.__doc__ = name, fn.__doc__
    return timed


def _report_cost():
    helpers = {name: {"calls": calls, "total_s": round(total, 3), "max_s": round(longest, 3),
                      **({"budget_s": _BUDGET_S[name]} if name in _BUDGET_S else {})}
               for name, (calls, total, longest) in _COST.items()}
    if helpers:
        _emit("cost", helpers=helpers)


if __name__ == "__main__":  # a routine run; the engine also loads this file to vet refs
    for _name in (*_BUDGET_S, *_WAITS):
        if callable(globals().get(_name)):
            globals()[_name] = _timed(_name, globals()[_name])
    atexit.register(_report_cost)
    sys.excepthook = _record_uncaught
