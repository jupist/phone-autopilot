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

A routine replays for weeks on a phone you have only seen once: lists reorder, apps reopen on other tabs,
toggles are already on, layouts and orientation change. Write it so that ordinary variation never needs a
repair, and never ask the user to rearrange their phone to suit the routine.

### How to act: one ladder for every step

For each action, use the highest rung that works on that screen:

1. **The system's own interface**, when the thing has one. Phone-wide state such as Do Not Disturb,
   volume or media playback has adb commands (`shell("cmd notification set_dnd priority")`), and many apps
   open straight to a screen through an intent or deep link. These work the same on every phone and every
   layout. Read the result back the same way (`shell("settings get global zen_mode")`). Change only what
   the user asked for. Before navigating an app to find out its state, look for the state in the system:
   `media_state()` says what any media app is playing and from which playlist or album, and `dumpsys`
   often holds the rest.
2. **The accessibility tree**: `tap_ui(label)` matches text, content description or resource id, and it
   sees icon buttons.
3. **Visible text**: `tap_text(label)`.
4. **The element's image**: `tap_image(ref)` searches the whole screen for a saved crop, so it still
   finds a tile, icon or button after the layout or orientation moved it. It is how to press things on
   screens that expose no text (Quick Settings, clocks, games, video).
5. **Coordinates, last**, and only inside a region an image checkpoint has just confirmed. Derive them
   from a found element or the screen bounds; never hardcode one per orientation or screen size.

When a rung is unavailable, step down instead of stopping. `screen()` raising `TreeUnavailable` means
the screen never settles (a clock, video, a playing track): use rung 4 and image checkpoints there. Never
treat that error as an empty screen; it says nothing about what is showing.

**Mind the cost.** Reading screen text (`screen()`, `find_text`, `tap_text`) takes about 2.5s, and about
12s before it fails on a screen that never settles. A system query, `current_app()` or a key press takes
under 0.2s, and an image check under 1s. So decide state with the cheapest reliable signal (is music
playing? ask the media session, not the screen), read the text once and reuse it, and when exploring, do
several steps per `phone_exec` call rather than one.

**Read where the time went.** Every run returns `time_spent`: each helper's total, costliest first, and
the time outside any helper (startup, sleeps, your own code). After a run, look at the top lines. A
helper called more often than the goal needs (asking the same question twice, a whole-screen
`find_image` where `checkpoint(image=...)` on the known region answers it, a sleep where a condition
would do) is the first thing to cut. `over_budget` means a helper itself was slower than it should
ever be: that is the plugin or the phone link, not your routine, so tell the user rather than working
around it.

### How to structure a routine

1. **Check the goal before the path.** Begin every phase by asking whether its goal already holds (the
   right screen is already open, the music is already playing, the setting is already on). If it holds,
   skip the navigation: apps resume where they were left, often exactly where the routine wants to be.
2. **Otherwise start from a known place.** Tap a fixed anchor (a bottom tab, Home) and checkpoint it,
   instead of assuming the launch screen.
3. **Fastest path first, sturdy fallback after.** If the target is visible, tap it. Otherwise scroll a
   little (`scroll_until`), then use the app's own search. Library order, feed position and "recently
   used" never matter then.
4. **Reach a state; never flip one.** Read the current state and act only if it is not the goal, so
   running the routine twice gives the same result. Branch on every state you saw while exploring
   (`Claim` / `Collected`, running / stopped).
5. **Wait for conditions, not time, and bound every loop.** Use `checkpoint`, `wait_for_text` or
   `wait_stable` instead of fixed `wait(2)` calls, and give every scroll or retry loop a maximum. For a
   condition the screen cannot show, use `checkpoint(name, check=callable)`. Never use a bare `assert`
   or the prelude's private helpers (`_emit`, `_miss`): only checkpoints leave a record a repair can use.
6. **Keep it short.** The user approves the routine before it runs, and the prompt has room for a
   summary and about 40 lines. A routine for a few goals should read in one screen; when it grows past
   that, look for a cheaper signal that makes the branches unnecessary. Keep `done()` to a short summary,
   not raw dumps.
7. **Leave the phone tidy.** Close what you opened along the way (the notification shade, dialogs,
   keyboards) and end on the screen the routine is for.
8. **A repair adds what is new and removes what is no longer needed.** When a checkpoint fails on a
   state you had not seen, add a branch for it and keep the path that already works. When a repair finds
   a simpler way to answer a question (a system signal instead of navigating to look), delete the old way
   rather than stacking the new one on top.
9. **Keep only paths that have run.** A branch no run has ever taken is a guess. Make each fallback
   happen once while you verify (put the phone in that state, then run), or leave it out and let a
   future failure add it with real evidence.

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

## Screens without text: games, clocks, Quick Settings

Canvas apps, animated screens and some system panels expose no text, so `screen()` and `find_text()`
return nothing. Work with them by image:

1. While exploring, call `screenshot()` in `phone_exec`. It prints a PNG path. Look at the image and pick a
   region of **stable** UI, such as the title or a button, as `[x, y, w, h]` in screenshot pixels. Scores,
   timers and tiles change, so they make bad refs.
2. On `save`, pass `refs`: `{"logo": {"screenshot": "<that path>", "region": [30, 490, 430, 160]}}`.
   Pick something distinctive: saving refuses a crop that is nearly blank or that appears twice on the
   same screen, because a search for it could land on the wrong one. Include an icon's label or border.
3. In the script, `checkpoint("game-open", image="logo")` passes when the region the ref was cut from
   matches it (`threshold=0.92` by default); a failed image checkpoint reports `similarity`. The default
   already tells one word from another; raising it makes checks fail on harmless rendering noise.
   `tap_image("play")` finds the ref anywhere on screen and taps its centre, and `find_image("play")`
   returns that point, or None, for branching. A `tap_image` miss fails like a checkpoint.
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
When any other app comes to the front, the watchdog presses Home and stops the script, and the phone pane
shows the user an Allow card for that app.

- `stopped: <app> is not on the allowed apps list`: tell the user which app and why the task needs it. They
  allow it with the card's Allow button or by typing `/routines allow <app>`. You cannot grant apps, so do
  not retry in that app until they say it is allowed.
- `stopped: Android is asking for a permission`: an "Allow ... ?" dialog is up. Ask the user to answer it on
  the phone. Never tap a permission dialog from a script.
