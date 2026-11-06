---
name: pocket-autopilot
description: "Save phone tasks as routines that replay without a model."
version: 0.1.0
author: Aman Jayesh (jupist), Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [phone, android, iphone, automation, routines]
    category: automation
    related_skills: [phone-harness]
---

# Pocket Autopilot Skill

A routine is a phone-harness script that replays a task with no model in the loop: explore the
task with `phone_exec`, save the steps that worked with the `routine` tool, verify by running
them, and repair them when an app changes. It does not decide what the user wants done; the user
approves every save and every first run through Hermes's approval prompt. Never try to get around
that prompt.

## When to Use

- The user wants a phone task done again later, on demand or on a schedule.
- A saved routine failed and needs a repair (the run result has `failed_checkpoint`).
- The user asks what routines exist, how one ran, or to run one.

## Prerequisites

- The phone-harness plugin, which provides `phone_exec`, with a phone connected
  (`ensure_device()` says what is missing).
- Apps the routine opens must be on the user's allowed apps list when one is set (see Pitfalls).

## How to Run

1. **Explore.** Use `phone_exec` to do the task step by step. Start with `ensure_device()`, read
   the screen with `screen()`, and keep only the steps that worked.
2. **Write a clean script.** Put a checkpoint after every screen change so a replay can verify
   where it is. Read values that vary per run (account, amount, search term) from `PARAMS`, never
   hardcode them. End with `done(...)`.
3. **Save.** Call `routine` with `action: "save"`, `name`, `code`, `title`, `app`, and `params`
   (the defaults for `PARAMS`). The user sees a summary of the code and approves it.
4. **Verify.** Call `routine` with `action: "run"` and check `ok`. A passing run is the test.
5. **Repair.** If a run fails, the result has `failed_checkpoint` (what was expected, `seen_text`,
   a `screenshot` path, and for an error the routine line that `raised`) and `next_steps`. Look at
   the screenshot, use `phone_exec` to bring the phone to the screen the checkpoint expects, write
   the fixed script, and `save` it as a new version. Then run it again. Each save is a new version
   the user approves; never patch the old one in place.

After you save or run a routine, print this on its own line so Hermes Desktop shows a card, with
the routine's real name:

```
::routine{name="daily-claim"}
```

## Quick Reference

`routine` actions: `save`, `run`, `list` (approval and last run), `show` (code and the last five
runs), `runs` (history), `delete` (only when the user asks). Names are lowercase letters, digits
and hyphens, up to 48 characters.

| Helper | What it does | Cost |
|---|---|---|
| `checkpoint(name, text= / any_of= / app=)` | waits for the screen, fails with evidence | one text read per poll |
| `checkpoint(name, check=callable)` | waits for state the screen cannot show | as the callable |
| `checkpoint(name, image=ref)` / `changed_from=(before, region)` | image checks | under 1s |
| `media_state(app)` | what is playing, from which playlist or album | ~0.1s |
| `shell(cmd)`, `current_app()`, key presses | system state and actions | under 0.2s |
| `tap_ui(label)`, `tap_text(label)`, `screen()`, `find_text()` | read or tap by text | ~2.5s, ~12s to fail on a screen that never settles |
| `tap_image(ref)`, `find_image(ref)` | find a saved crop anywhere on screen | under 1s |

Every run returns `time_spent`: each helper's total, costliest first, and the time outside any
helper (startup, sleeps, your own code).

## Procedure

A routine replays for weeks on a phone you have seen once: lists reorder, apps reopen on other
tabs, toggles are already on, layouts and orientation change. Write it so that ordinary variation
never needs a repair, and never ask the user to rearrange their phone to suit the routine.

### Act through the sturdiest handle

For each action, use the highest rung that works on that screen:

1. **The system's own interface**, when the thing has one. Phone-wide state such as Do Not
   Disturb, volume or media playback has adb commands (`shell("cmd notification set_dnd
   priority")`), and many apps open straight to a screen through an intent or deep link. These
   work the same on every phone and layout. Read the result back the same way
   (`shell("settings get global zen_mode")`). Change only what the user asked for. Before
   navigating an app to find out its state, look for it in the system: `media_state()` says what
   any media app is playing and from which playlist or album, and `dumpsys` often holds the rest.
2. **The accessibility tree**: `tap_ui(label)` matches text, content description or resource id,
   and it sees icon buttons.
3. **Visible text**: `tap_text(label)`.
4. **The element's image**: `tap_image(ref)` searches the whole screen for a saved crop, so it
   still finds a tile, icon or button after the layout moved it. It is how to press things on
   screens that expose no text (Quick Settings, clocks, games, video).
5. **Coordinates, last**, and only inside a region an image checkpoint has just confirmed. Derive
   them from a found element or the screen bounds; never hardcode one per orientation or size.

When a rung is unavailable, step down instead of stopping. `screen()` raising `TreeUnavailable`
means the screen never settles (a clock, video, a playing track): use rung 4 and image checkpoints
there. That error says nothing about what is showing; never treat it as an empty screen.

Decide state with the cheapest reliable signal (is music playing? ask the media session, not the
screen), read the text once and reuse it, and when exploring, do several steps per `phone_exec`
call rather than one.

### Structure the routine

1. **Check the goal before the path.** Begin every phase by asking whether its goal already
   holds (the right screen is open, the music is playing, the setting is on). If it holds, skip
   the navigation: apps resume where they were left, often where the routine wants to be.
2. **Otherwise start from a known place.** Tap a fixed anchor (a bottom tab, Home) and checkpoint
   it, instead of assuming the launch screen.
3. **Fastest path first, sturdy fallback after.** If the target is visible, tap it. Otherwise
   scroll a little (`scroll_until`), then use the app's own search. Library order, feed position
   and "recently used" never matter then.
4. **Reach a state; never flip one.** Read the current state and act only if it is not the goal,
   so running the routine twice gives the same result. Branch on every state you saw while
   exploring (`Claim` / `Collected`, running / stopped).
5. **Wait for conditions, not time, and bound every loop.** Use `checkpoint`, `wait_for_text` or
   `wait_stable` instead of fixed `wait(2)` calls, and give every scroll or retry loop a maximum.
   For a condition the screen cannot show, use `checkpoint(name, check=callable)`. Never use a
   bare `assert` or the prelude's private helpers (`_emit`, `_miss`). A checkpoint before each
   action says what the screen should be, so check for the content you are about to use (a
   result, not the query you typed).
6. **Keep it short.** The approval prompt has room for a summary and about 40 lines. A routine for
   a few goals should read in one screen; when it grows past that, look for a cheaper signal that
   makes the branches unnecessary. Keep `done()` to a short summary, not raw dumps.
7. **Leave the phone tidy.** Close what you opened along the way (the notification shade,
   dialogs, keyboards) and end on the screen the routine is for.
8. **A repair adds what is new and removes what is no longer needed.** When a checkpoint fails on
   a state you had not seen, add a branch for it and keep the path that already works. When a
   repair finds a simpler way to answer a question, delete the old way rather than stacking the
   new one on top.
9. **Keep only paths that have run.** A branch no run has taken is a guess. Make each fallback
   happen once while you verify (put the phone in that state, then run), or leave it out and let
   a future failure add it with real evidence.

```python
# daily-claim: claim the daily reward in com.example.app. params: {"account": "default"}
ensure_device()
open_app("com.example.app")
checkpoint("home", text="Rewards", app="com.example.app", timeout=15)
tap_ui("Rewards")
checkpoint("reward-screen", any_of=["Claim", "Collected"], timeout=10)
if find_text("Claim"):
    tap_ui("Claim")
checkpoint("claimed", any_of=["Collected", "Come back tomorrow"], timeout=10)
done(claimed=True, account=PARAMS["account"])
```

### Screens without text

Canvas apps, animated screens and some system panels expose no text, so `screen()` and
`find_text()` return nothing. Work with them by image:

1. While exploring, call `screenshot()` in `phone_exec`. It prints a PNG path. Look at the image
   and pick a region of **stable** UI, such as a title or a button, as `[x, y, w, h]` in
   screenshot pixels. Scores, timers and tiles change, so they make bad refs.
2. On `save`, pass `refs`: `{"logo": {"screenshot": "<that path>", "region": [30, 490, 430, 160]}}`.
   Saving refuses a crop that is nearly blank or that appears twice on the same screen, because a
   search for it could land on the wrong one. Include an icon's label or border.
3. `checkpoint("game-open", image="logo")` passes when the region the ref was cut from matches it
   (`threshold=0.92` by default; a failure reports `similarity`). The default already tells one
   word from another, so leave it. `tap_image("play")` finds the ref anywhere and taps its
   centre; `find_image("play")` returns that point, or None, for branching.
4. To confirm an action landed, `before = snap(region)`, act, then
   `checkpoint("moved", changed_from=(before, region))`. It polls until the region differs. Use
   the bare `changed(before, region)` only to branch.

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
done(moves=len(PARAMS["moves"]))
```

## Pitfalls

- **Allowed apps.** When the user set an allowed apps list, a watchdog stops any script that
  brings another app to the front and the phone pane shows the user an Allow card.
  `stopped: <app> is not on the allowed apps list`: tell the user which app and why the task
  needs it. They allow it with the card or `/routines allow <app>`; you cannot grant apps, so do
  not retry in that app until they say it is allowed.
- **Permission dialogs.** `stopped: Android is asking for a permission` means an "Allow ... ?"
  dialog is up. Ask the user to answer it on the phone. Never tap a permission dialog from a
  script.
- **Imports.** Use only the phone-harness helpers; a routine runs on this computer, and the
  approval summary lists any import.
- **Sensitive actions.** Never type passwords, PINs or 2FA codes, and never send, post, buy or
  delete without the user's explicit yes.

## Verification

- The run you just saved passes with `ok: true`, from the state the user described and again
  with every goal already met (that run should skip every phase).
- Read `time_spent`. A helper called more often than the goal needs (asking the same question
  twice, a whole-screen `find_image` where `checkpoint(image=...)` on the known region answers it,
  a sleep where a condition would do) is the first thing to cut. `over_budget` means a helper
  itself was slower than it should ever be: that is the plugin or the phone link, not your
  routine, so tell the user rather than working around it.
