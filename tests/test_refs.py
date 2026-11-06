"""Image refs: crops saved with a routine and image checkpoints for canvas apps."""
import pytest
from PIL import Image


def _png(path, size=(200, 400), color="white", box=None, box_color="red"):
    img = Image.new("RGB", size, color)
    if box:
        x, y, w, h = box
        img.paste(Image.new("RGB", (w, h), box_color), (x, y))
    img.save(path)
    return str(path)


def test_save_crops_refs_into_the_routine(engine, data_dir, tmp_path):
    shot = _png(tmp_path / "shot.png", box=(10, 20, 50, 30))
    meta = engine.save_routine("game", "done()", refs={"logo": {"screenshot": shot, "region": [5, 15, 60, 40]}})
    assert meta["refs"] == {"logo": {"region": [5, 15, 60, 40]}}
    with Image.open(data_dir / "routines" / "game" / "refs" / "logo.png") as crop:
        assert crop.size == (60, 40)
        assert crop.getpixel((10, 10)) == (255, 0, 0)


@pytest.mark.parametrize("spec, message", [
    ({"region": [0, 0, 50, 30]}, "not found"),
    ({"screenshot": "SHOT", "region": [190, 0, 50, 30]}, "outside"),
    ({"screenshot": "SHOT", "region": [0, 0, 4, 4]}, "at least 8x8"),
    ({"screenshot": "SHOT", "region": [0, 0, 50]}, "[x, y, w, h]"),
])
def test_bad_refs_are_rejected_before_anything_is_saved(engine, data_dir, tmp_path, spec, message):
    shot = _png(tmp_path / "shot.png")
    spec = {k: (shot if v == "SHOT" else v) for k, v in spec.items()}
    with pytest.raises(engine.RoutineError, match=message):
        engine.save_routine("game", "done()", refs={"logo": spec})
    assert not (data_dir / "routines" / "game" / "routine.json").exists()


def _routine_with_logo(engine, tmp_path, code):
    shot = _png(tmp_path / "ref.png", box=(10, 20, 50, 30))
    # The button with a margin of background: a solid crop would be refused as blank.
    meta = engine.save_routine("game", code, refs={"logo": {"screenshot": shot, "region": [5, 15, 60, 40]}})
    engine.approve("game", meta["sha256"])


def test_image_checkpoint_passes_on_the_matching_screen(engine, phone, tmp_path):
    same = _png(tmp_path / "live.png", box=(10, 20, 50, 30))
    phone.set_frames([{"app": "com.example.app", "texts": [], "image": same}])
    _routine_with_logo(engine, tmp_path, 'checkpoint("in game", image="logo", timeout=2)\ndone()\n')
    record = engine.run_routine("game", timeout_s=30)
    assert record["ok"] is True, record["error"]


def test_image_checkpoint_fails_with_a_similarity_score(engine, phone, tmp_path):
    other = _png(tmp_path / "live.png", color="black")
    phone.set_frames([{"app": "com.example.app", "texts": [], "image": other}])
    _routine_with_logo(engine, tmp_path, 'checkpoint("in game", image="logo", timeout=1)\ndone()\n')
    record = engine.run_routine("game", timeout_s=30)
    failed = record["failed_checkpoint"]
    assert record["ok"] is False
    assert failed["expected"] == "screen matching ref 'logo'"
    assert failed["similarity"] < 0.5


def _snap_then_check(engine, tmp_path):
    code = ('before = snap([10, 20, 50, 30])\n'
            'checkpoint("moved", changed_from=(before, [10, 20, 50, 30]), timeout=2)\n'
            'done()\n')
    meta = engine.save_routine("game", code)
    engine.approve("game", meta["sha256"])


def test_changed_from_checkpoint_passes_once_the_region_changes(engine, phone, tmp_path):
    first = _png(tmp_path / "a.png", box=(10, 20, 50, 30))
    second = _png(tmp_path / "b.png", box=(10, 20, 50, 30), box_color="blue")
    phone.set_frames([{"app": "com.example.app", "texts": [], "image": first},
                      {"app": "com.example.app", "texts": [], "image": second}])
    _snap_then_check(engine, tmp_path)
    record = engine.run_routine("game", timeout_s=30)
    assert record["ok"] is True, record["error"]


def test_a_move_that_never_lands_is_a_failed_checkpoint(engine, phone, tmp_path):
    still = _png(tmp_path / "a.png", box=(10, 20, 50, 30))
    phone.set_frames([{"app": "com.example.app", "texts": [], "image": still}])
    _snap_then_check(engine, tmp_path)
    record = engine.run_routine("game", timeout_s=30)
    assert record["ok"] is False
    assert record["failed_checkpoint"]["name"] == "moved"
    assert record["failed_checkpoint"]["expected"] == "region [10, 20, 50, 30] changed"


def test_tap_image_finds_a_ref_wherever_the_layout_moved_it(engine, phone, tmp_path):
    # Saved at (10, 20); on today's screen the same red button sits at (120, 300).
    moved = _png(tmp_path / "moved.png", box=(120, 300, 50, 30))
    phone.set_frames([{"app": "com.example.app", "texts": [], "image": moved}])
    _routine_with_logo(engine, tmp_path, 'tap_image("logo", timeout=2)\ndone()\n')
    record = engine.run_routine("game", timeout_s=30)
    assert record["ok"] is True, record["error"]
    assert "TAP 145 315" in record["output_tail"]  # the centre of the moved button


def test_tap_image_miss_fails_like_a_checkpoint(engine, phone, tmp_path):
    blank = _png(tmp_path / "blank.png", color="black")
    phone.set_frames([{"app": "com.example.app", "texts": [], "image": blank}])
    _routine_with_logo(engine, tmp_path, 'tap_image("logo", timeout=1)\ndone()\n')
    record = engine.run_routine("game", timeout_s=30)
    assert record["ok"] is False
    assert record["failed_checkpoint"]["name"] == "tap logo"
    assert record["failed_checkpoint"]["expected"] == "'logo' anywhere on screen"
    assert "TAP" not in record["output_tail"]


def test_a_blank_ref_is_refused(engine, tmp_path):
    shot = _png(tmp_path / "shot.png", box=(10, 20, 50, 30))
    with pytest.raises(engine.RoutineError, match="nearly blank"):
        engine.save_routine("game", "done()", refs={"logo": {"screenshot": shot, "region": [10, 20, 50, 30]}})


def test_a_ref_that_appears_twice_is_refused(engine, tmp_path):
    # Two identical buttons: a search for one could land on the other.
    from PIL import Image
    img = Image.new("RGB", (200, 400), "white")
    for y in (20, 200):
        img.paste(Image.new("RGB", (50, 30), "red"), (10, y))
    img.save(tmp_path / "twins.png")
    with pytest.raises(engine.RoutineError, match=r"also appears at \[5, 195\]"):
        engine.save_routine("game", "done()", refs={"logo": {"screenshot": str(tmp_path / "twins.png"),
                                                             "region": [5, 15, 60, 40]}})


def _label(path, word, at=(20, 40)):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (400, 200), (12, 12, 12))
    ImageDraw.Draw(img).text(at, word, fill=(200, 200, 200), font=ImageFont.load_default(size=48))
    img.save(path)
    return str(path)


def _label_routine(engine, tmp_path):
    shot = _label(tmp_path / "ref.png", "Calendar")
    meta = engine.save_routine("labels", 'checkpoint("on-calendar", image="title", timeout=1)\ndone()\n',
                               refs={"title": {"screenshot": shot, "region": [10, 30, 300, 80]}})
    engine.approve("labels", meta["sha256"])


def test_an_image_check_tells_one_word_from_another(engine, phone, tmp_path):
    # Same panel, same place, one word different: the old 48x48 compare called these 0.75 alike.
    phone.set_frames([{"app": "com.example.app", "texts": [], "image": _label(tmp_path / "live.png", "Weather")}])
    _label_routine(engine, tmp_path)
    record = engine.run_routine("labels", timeout_s=30)
    assert record["ok"] is False
    assert record["failed_checkpoint"]["similarity"] < 0.6


def test_an_image_check_ignores_a_two_pixel_shift(engine, phone, tmp_path):
    live = _label(tmp_path / "live.png", "Calendar", at=(22, 42))
    phone.set_frames([{"app": "com.example.app", "texts": [], "image": live}])
    _label_routine(engine, tmp_path)
    record = engine.run_routine("labels", timeout_s=30)
    assert record["ok"] is True, record["failed_checkpoint"]


def test_dotted_text_on_an_oled_style_screen_still_matches(engine, phone, tmp_path):
    # Real crops from Focus Dock: the same "DAILY GOAL" title, its dot pattern shifted between shots.
    from pathlib import Path
    from PIL import Image
    assets = Path(__file__).parent / "assets"
    ref_shot, live_shot = tmp_path / "ref.png", tmp_path / "live.png"
    for src, dst in ((assets / "dotted-ref.png", ref_shot), (assets / "dotted-live.png", live_shot)):
        canvas = Image.new("RGB", (400, 120), (0, 0, 0))
        canvas.paste(Image.open(src), (20, 30))
        canvas.save(dst)
    meta = engine.save_routine("dots", 'checkpoint("title", image="title", threshold=0.98, timeout=1)\ndone()\n',
                               refs={"title": {"screenshot": str(ref_shot), "region": [20, 30, 310, 50]}})
    engine.approve("dots", meta["sha256"])
    phone.set_frames([{"app": "com.example.app", "texts": [], "image": str(live_shot)}])
    record = engine.run_routine("dots", timeout_s=30)
    assert record["ok"] is True, record["failed_checkpoint"]
