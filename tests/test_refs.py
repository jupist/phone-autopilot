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
    meta = engine.save_routine("game", "done()", refs={"logo": {"screenshot": shot, "region": [10, 20, 50, 30]}})
    assert meta["refs"] == {"logo": {"region": [10, 20, 50, 30]}}
    with Image.open(data_dir / "routines" / "game" / "refs" / "logo.png") as crop:
        assert crop.size == (50, 30)
        assert crop.getpixel((5, 5)) == (255, 0, 0)


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
    meta = engine.save_routine("game", code, refs={"logo": {"screenshot": shot, "region": [10, 20, 50, 30]}})
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
