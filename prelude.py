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
_FIT_SIDE = 240
_ALIGN_PX = 2
_INK_FLOOR = 0.05


def _fit(img, size=None):
    from PIL import Image

    img = img.convert("RGB")
    if size is None:
        scale = min(1.0, _FIT_SIDE / max(img.size))
        size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(size, Image.BOX)


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


def _outside(a, b):
    """a's pixels with no close colour anywhere in b's 3x3 neighbourhood."""
    from PIL import ImageChops, ImageFilter

    low, high = b.filter(ImageFilter.MinFilter(3)), b.filter(ImageFilter.MaxFilter(3))
    return _over(ImageChops.lighter(ImageChops.subtract(low, a), ImageChops.subtract(a, high)))


def _similarity(a, b):
    """1.0 for the same content, low for different content, even when only a word differs."""
    from PIL import ImageChops

    a = _fit(a)
    b = _fit(b, a.size)
    ink = _count(ImageChops.lighter(_ink(a), _ink(b)))
    differing = min(
        _count(ImageChops.lighter(_outside(a, moved), _outside(moved, a)))
        for moved in (ImageChops.offset(b, dx, dy)
                      for dx in range(-_ALIGN_PX, _ALIGN_PX + 1) for dy in range(-_ALIGN_PX, _ALIGN_PX + 1)))
    return max(0.0, 1.0 - differing / max(ink, a.width * a.height * _INK_FLOOR))


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


def _coarse_hits(screen, ref, f):
    from PIL import Image, ImageChops, ImageStat

    small = screen.resize((max(1, screen.width // f), max(1, screen.height // f)), Image.BOX)
    tpl = ref.resize((max(1, ref.width // f), max(1, ref.height // f)), Image.BOX)
    hits = []
    for y in range(small.height - tpl.height + 1):
        for x in range(small.width - tpl.width + 1):
            box = (x, y, x + tpl.width, y + tpl.height)
            hits.append((ImageStat.Stat(ImageChops.difference(small.crop(box), tpl)).sum[0], x * f, y * f))
    return sorted(hits)[:_SEARCH_KEEP]


def _refine(screen, ref, x0, y0, f):
    from PIL import ImageChops, ImageStat

    best = None
    for y in range(max(0, y0 - f), min(screen.height - ref.height, y0 + f) + 1):
        for x in range(max(0, x0 - f), min(screen.width - ref.width, x0 + f) + 1):
            box = (x, y, x + ref.width, y + ref.height)
            score = ImageStat.Stat(ImageChops.difference(screen.crop(box), ref)).sum[0]
            if best is None or score < best[0]:
                best = (score, x, y)
    return best


def _locate(screen, ref):
    """Top-left of the best match of ref in screen and its similarity, or None if it cannot fit."""
    if ref.width > screen.width or ref.height > screen.height:
        return None
    gray, tpl = screen.convert("L"), ref.convert("L")
    f = max(1, min(ref.width, ref.height) // 12, -(-max(screen.size) // _SEARCH_MAX_SIDE))
    found = []
    for _, x, y in _coarse_hits(gray, tpl, f):
        refined = _refine(gray, tpl, x, y, f)
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


def _matches(text, any_of, app, exact, image=None, threshold=MATCH, changed_from=None):
    """Cheapest condition first: the foreground app (~0.06s), images (~0.7s), then one text read."""
    if app is not None and not _in_app(app):
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
    while True:
        try:
            if _matches(text, any_of, app, exact, image, threshold, changed_from):
                break
        except _NoText:
            _miss(name, _expected(text, any_of, app, image, changed_from)
                  + " (this screen never settles, so it has no readable text: check it with an image ref)",
                  read_text=False)
        if time.time() >= deadline:
            extra = {"similarity": _probe(lambda: looks_like(image, threshold)[1])} if image else {}
            _miss(name, _expected(text, any_of, app, image, changed_from), **extra)
        time.sleep(POLL_S)
    _emit("checkpoint", name=name, status="passed")


def done(**summary):
    """Report the routine's outcome; the last event of a successful run."""
    _emit("done", **summary)
