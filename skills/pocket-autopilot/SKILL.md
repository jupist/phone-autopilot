---
name: pocket-autopilot
description: "Teach the phone a task once, save it as a routine, replay it without the model, and repair it when a screen changes. Use with the routine tool and phone_exec."
---

# Pocket Autopilot: phone routines

A routine is a phone-harness script that replays a task with no model in the loop. You
explore the task with `phone_exec`, save the steps that worked, verify by running them, and
repair them when an app changes. The user approves every save and every first run through
Hermes's approval prompt. Never try to get around that prompt.

## Workflow

1. **Explore.** Use `phone_exec` to do the task step by step. Start with `ensure_device()`,
   read the screen with `screen()`, and keep only the steps that worked.
2. **Write a clean script.** Put `checkpoint(name, text=...)` after every screen change so a
   replay can verify where it is. Read values that vary per run (account, amount, search term)
   from `PARAMS`, never hardcode them. End with `done(...)`.
3. **Save.** Call `routine` with `action: "save"`, `name`, `code`, `title`, `app`, and
   `params` (the defaults for `PARAMS`). The user sees the code and approves it.
4. **Verify.** Call `routine` with `action: "run"` and check `ok`. A passing run is the test.
5. **Repair.** If a run fails, the result has `failed_checkpoint` (expected text, `seen_text`,
   and a `screenshot` path) and `next_steps`. Look at the screenshot, use `phone_exec` to bring
   the phone to the screen the checkpoint expects, write the fixed script, and `save` it as a
   new version. Then run it again. Do not patch the old version in place: each save is a new
   version the user approves.

After you save or run a routine, print this on its own line so Hermes Desktop shows a card:

```
::routine{name="daily-claim"}
```

Use the routine's real name in place of `daily-claim`.

## Other actions

- `list`: all routines, with approval and last run.
- `show` (name): code, metadata, and the last five runs.
- `runs` (name, limit): run history.
- `delete` (name): removes the routine. Only do this when the user asks.

Names are lowercase letters, digits, and hyphens, up to 48 characters.

## Script rules

- Use only the phone-harness helpers. Import nothing else.
- Each checkpoint has a stable name, such as `home`, `reward-screen`, `done-toast`.
- Check for a specific text, not a visual guess. Prefer `text=` or `any_of=`, and `app=` when the app matters.
- Do not type passwords, PINs, or 2FA codes. Do not send, post, buy, or delete without the user's explicit yes.

## Make routines survive change

A routine replays for weeks while lists reorder, apps reopen on other tabs, and toggles are already
on. Write it so that ordinary variation never needs a repair.

1. **Check the goal before the path.** Begin every phase by asking whether its goal already holds:
   the timer is already on the Pomodoro screen, the music is already playing, Do Not Disturb is
   already on. If it holds, skip the navigation. Apps resume where they were left, and that is often
   exactly where the routine wants to be.
2. **Otherwise start from a known place.** When the goal does not hold, tap a fixed anchor (a bottom
   tab, Home) by its label and checkpoint it, instead of assuming the launch screen.
3. **Find elements by meaning, not position.** In order of preference: `tap_ui(label)` (the
   accessibility tree: text, content description or resource id, and it sees icon buttons),
   `tap_text(label)` (visible text), an image ref, and raw coordinates last. Use coordinates only on a
   canvas, and only after an image checkpoint has confirmed the screen. Never hardcode a coordinate per
   orientation or screen size: derive it from a found element or from the screen bounds.
4. **Fastest path first, sturdy fallback after.** If the target is visible, tap it. Otherwise scroll a
   little (`scroll_until`), and then fall back to the app's own search. Library order, feed position
   and "recently used" never matter then.
5. **Reach a state; never flip one.** A toggle or timer can already be on. Read the current state
   and act only if it is not the goal, so running the routine twice gives the same result. When the
   screen cannot show the state, a read-only system query can (`shell("settings get global zen_mode")`).
   Branch on every state you saw while exploring (`Claim` / `Collected`, running / stopped).
6. **Ask the screen questions it can answer.** Animated screens (clocks, video, games) never settle, so
   `screen()` returns no text there and text checkpoints always time out. Use image refs or
   `changed_from` on those screens.
7. **Wait for conditions, not time, and bound every loop.** Use `checkpoint`, `wait_for_text` or
   `wait_stable` instead of fixed `wait(2)` calls, and give every scroll or retry loop a maximum.
8. **Leave the phone tidy.** Close what you opened along the way (the notification shade, dialogs,
   keyboards) and end on the screen the routine is for.
9. **A repair adds; it does not replace.** When a checkpoint fails on a state you had not seen, add a
   branch for it and keep the path that already worked.

```python
# The playlist step from a routine: anchor, then visible → search.
# Labels are the ones read from screen() while exploring.
open_app("com.spotify.music")
tap_ui("Your Library")
checkpoint("library", text="Your Library", app="com.spotify.music")
playlist = PARAMS["playlist"]
if not find_text(playlist, exact=True):
    tap_ui("Search")
    tap_text("What do you want to listen to?")
    type_text(playlist)
    wait_for_text(playlist, exact=True)
tap_text(playlist, exact=True)
checkpoint("playlist-open", text=playlist)
```

## Example routine

```python
# daily-claim: claim the daily reward in com.example.app.
ensure_device()
open_app("com.example.app")
wait_stable()
checkpoint("home", text="Rewards", app="com.example.app", timeout=15)
tap_text("Rewards")
wait_stable()
checkpoint("reward-screen", any_of=["Claim", "Collected"], timeout=10)
if find_text("Claim"):
    tap_text("Claim")
    wait_stable()
checkpoint("claimed", any_of=["Collected", "Come back tomorrow"], timeout=10)
done(claimed=True, account=PARAMS.get("account", "default"))
```

Its defaults go in `params`, for example `{"account": "default"}`.

## Games and other canvas apps

Many games draw on a canvas, so `screen()` and `find_text()` return nothing. Verify them by image:

1. While exploring, call `screenshot()` in `phone_exec`. It prints a PNG path. Look at the image and pick a
   region of **stable** UI, such as the title or a button, as `[x, y, w, h]` in screenshot pixels. Scores,
   timers and tiles change, so they make bad refs.
2. On `save`, pass `refs`: `{"logo": {"screenshot": "<that path>", "region": [30, 490, 430, 160]}}`.
3. In the script, `checkpoint("game-open", image="logo")` passes when the live region matches the ref
   (`threshold=0.92` by default). A failed image checkpoint reports `similarity`.
4. To confirm an action landed, `before = snap(region)`, act, then
   `checkpoint("moved", changed_from=(before, region))`. It polls until the region differs, and a move that
   never lands fails like any checkpoint, with a screenshot. Use the bare `changed(before, region)` only to
   branch, never `raise` on it: a plain exception leaves no failure record to heal from.

```python
# 2048-moves: make a few moves in 2048 and confirm the board changed.
ensure_device()
open_app("com.tpcstld.twozerogame")
checkpoint("game-open", image="logo", timeout=10)
board = PARAMS["board"]            # [x, y, w, h] of the grid
for direction in PARAMS["moves"]:  # e.g. ["up", "left", "down", "right"]
    before = snap(board)
    swipe(direction, at=(540, 1325))
    checkpoint("moved " + direction, changed_from=(before, board), timeout=3)
checkpoint("still-in-game", image="logo", timeout=5)
done(moves=len(PARAMS["moves"]))
```

## App allowlist

If the user set `allowed_apps` for this plugin, every `phone_exec` script and routine runs with a watchdog.
When any other app comes to the front, the watchdog presses Home and stops the script. If you see
`stopped: <app> is not on the allowed apps list`, do not retry in that app. Tell the user, and they can add
it to the allowlist.
