# Phone Autopilot

Teach your phone a task once, then replay it without the model.

Phone Autopilot is a Hermes plugin for phone routines. The agent explores a
task on your phone with `phone_exec`, and once it works, saves the steps as a
**routine**. Routines replay with no LLM in the loop, verify themselves with
**checkpoints** (a text or app check after every screen change), and ask the
agent to heal them when an app changes. A routine can run every day through the
`daily-routine` automation blueprint. The Desktop pane shows the live phone,
your routines and a step-by-step run log.

## Requirements

- Hermes Agent `>=0.21.6`
- The [phone-harness](https://github.com/phone-harness/phone-harness) plugin,
  which gives the agent `phone_exec`. Phone Autopilot installs the
  `phone-harness` Python package it runs routines with, but not that plugin.
- An Android phone with USB debugging on and `adb` on your PATH. phone-harness
  also drives iPhones and cloud phones; Phone Autopilot is built and tested
  on Android, and its system helpers (`shell`, `media_state`) and app
  allowlist need Android.
- Optional: [`scrcpy`](https://github.com/Genymobile/scrcpy) for the pop-out window

## Install

```bash
hermes plugins install phone-harness
hermes plugins install https://github.com/jupist/phone-autopilot
```

Once it is in the plugin catalog: `hermes plugins install phone-autopilot`.

## Quick start

Plug in your phone, then talk to Hermes in a chat:

> Open the shop app and claim today's reward. Save it as a routine called `daily-claim`.

The agent drives the phone with `phone_exec` until the task works, then calls
the `routine` tool to save the script. You approve the save, and the routine
is ready to run:

> Run `daily-claim`.

Routines use `PARAMS` for values that change between runs, and every screen
transition has a checkpoint so a failure stops at the right place with a
screenshot. A step that fails some other way (text that is not there to tap,
an app that will not launch) leaves the same record: a screenshot, the visible
text, and the routine line that raised.

To run a routine every morning, add the blueprint:

```
/blueprint phone-autopilot:daily-routine
```

Other commands: `/routines` lists routines and their last run, and
`/routines show <name>` prints the code and recent runs.

## Screens without text

Games, clocks, video and some system panels (Quick Settings) expose no text,
so a routine works with them by image. When saving, the agent passes
`refs={"logo": {"screenshot": ..., "region": [x, y, w, h]}}` and the plugin
crops that region into `refs/logo.png`. A crop that is nearly blank, or that
appears twice on the same screen, is refused, because a search for it could
land on the wrong spot.

- `checkpoint("game open", image="logo")` compares the region the ref came
  from with the saved crop.
- `tap_image("play")` searches the whole screen for the ref and taps its
  centre, so it still works after the layout or orientation moves the button.
  `find_image("play")` returns that point, or `None`.
- `before = snap(board)` then `checkpoint("moved", changed_from=(before, board))`
  confirms that a move landed.

Image search runs on Pillow alone and takes under a second on a phone-sized
screen.

## App allowlist

Choose which apps routines and `phone_exec` may use. With at least one app
allowed, every script runs with a watchdog that checks the foreground app. If
anything else comes to the front (apart from the launcher and system UI), it
presses Home and stops the script.

You don't have to list apps up front. The first time a script reaches a new
app, the phone pane shows a card: **Routine deep-work tried to open an app:
com.spotify.music [Allow] [Not now]**. One click allows it from then on. In
chat, type the same thing as a command:

```
/routines allow com.spotify.music
/routines revoke com.spotify.music
/routines apps
```

Only you can allow an app. The agent has no tool for it, so even with
approvals off (`/yolo`) the plugin gives it no way to widen the list. The
Allow button saves through Hermes's plugin settings, the same call as the
Settings form, and the slash command through the plugin's own settings
writer. It is a guard rail, not a sandbox: an agent with shell access could
still edit `config.yaml` directly. When Android shows an "Allow … ?"
permission dialog, the script stops and leaves the dialog for you to answer
on the phone.

The list lives in `config.yaml` under
`plugins.entries.phone-autopilot.settings.allowed_apps` and also shows in
Settings → Plugins. An empty list (the default) turns the guard off.

## Where the time goes

Every run measures each helper it calls and reports the totals, costliest
first, plus the time spent outside any helper (startup, sleeps, the routine's
own code):

```
find_image: 3.52s (1 call)
media_state: 2.41s (4 calls, up to 0.66s each)
outside helpers (startup, sleeps, the routine's own code): 0.40s
```

The agent gets this as `time_spent` with every run and uses it to cut calls
the goal does not need. The run log in the pane shows it too. Helpers that
should answer at once (a screen read, a shell command, an image search) have
a budget; a call over it shows as `over_budget`, which points at the plugin
or the phone link rather than the routine. The test suite holds the plugin's
own image search to that budget on a phone-sized screen.

## How approval works

- **Saving** a routine, and **running** one that has not been approved yet,
  goes through the normal Hermes approval prompt. The prompt summarises the
  whole routine, read from its code without running it: the apps it opens,
  every adb shell command, what it types and taps, and anything it imports
  (routines run on this computer). The first 40 lines of code follow. Nothing
  new runs on your phone without your yes.
- Once approved, a routine runs without prompts. Editing it (a new version)
  requires approval again.
- **Scheduled runs never self-repair.** If a checkpoint misses unattended, the
  run stops, keeps the failure screenshot, and reports which checkpoint failed.
  Reply `fix <routine>` in a chat where you can approve the repair.

## Desktop pane

With Hermes Desktop (or `hermes webapp`), a **Phone Autopilot** pane sits on
the right. Like every user plugin, its Desktop half starts off: turn it on in
**Capabilities → Plugins → Phone Autopilot → Desktop**.

- The live phone screen. Click to tap; the tap point is mapped onto the
  phone's real resolution. Home, Back and Recents buttons appear when the
  device supports them.
- Pause the live view, or pop the phone out with scrcpy.
- Routines with version, approval state, last run and a Run button. Run is
  disabled until a routine is approved.
- A run log that fills in as each step and checkpoint happens. A missed
  checkpoint shows what it expected, what was on screen, and the screenshot path.

The status bar shows `📱 N routines` and the last result. The transcript
renders `::routine{name="daily-claim"}` as a card with a Run button and the
routine's code.

## Disclosure

- **Code it runs.** Routines are Python written by the model. Each run starts
  a `python -m phone_harness.run` child process on **this computer** with your
  user's permissions; it taps and types on your **real phone** over adb and runs
  the routine's `shell()` commands there. You approve every saved version first,
  and the approval summary lists every app, shell command and import. The
  child runs in its own process group and is killed at the run's timeout
  (5 minutes by default).
- **Hooks.** `pre_tool_call` puts saves and unapproved runs through Hermes's
  approval prompt and, when an allowlist is set, prepends the app watchdog to
  `phone_exec` code (a `modify` directive). The watchdog is a thread inside the
  script that polls the foreground app. `post_tool_call` reads `phone_exec`
  output to raise an Allow card for a blocked app.
- **Files.** It writes under `<HERMES_HOME>/plugin-data/phone-autopilot/`:
  routines and their versions, image refs, run records with failure
  screenshots, pending app requests and a lock file. Outside it, it reads
  `config.yaml` (`allowed_apps`), the screenshot files the agent names when
  saving image refs, and phone-harness's own failure screenshots, which it
  copies into the run record. It changes `config.yaml` only through Hermes's
  plugin settings writers, when you allow or revoke an app. The live view is
  kept in memory, not on disk.
- **Network.** The plugin makes no network calls of its own. With a phone
  over USB, nothing leaves this computer. With a phone-harness cloud phone,
  routines and the live view reach it through phone-harness's own connection
  to Phone Harness Cloud.
- **Telemetry.** None of its own. phone-harness has PostHog telemetry; routine
  runs start it with `DO_NOT_TRACK=1`, so it stays off for them.
- **Other processes.** `scrcpy` starts only when you click Pop out, if it is
  installed, and keeps running until you close its window.
- **Secrets.** Run environments do not receive variables whose names end in
  `_KEY`, `_TOKEN`, `_SECRET` or `_PASSWORD`. The plugin stores no credentials.

## How it works

```
 chat / cron ──► agent ──► routine tool ──► approval prompt (Hermes)
                              │
                              ▼
                    engine: prelude + routine.py
                              │  subprocess
                              ▼
                 phone_harness.run ──► phone (adb)
                              │
              @@AUTOPILOT events on stdout
                              │
          ┌───────────────────┴───────────────────┐
          ▼                                       ▼
  run record on disk                 gateway event stream
  (plugin-data/runs/…)               plugin.phone-autopilot.run.*
                                                  │
                                                  ▼
                                    Desktop pane, chip, transcript card

 Desktop pane ──► dashboard API (/api/plugins/phone-autopilot)
                  /status /frame /tap /key /routines /runs …
```

## Development

```bash
uv run --no-project --with pytest --with fastapi --with httpx --with pillow \
  python -m pytest tests -q
node tests/js/run_check.mjs
hermes plugins validate . --install-deps
```

The Python tests run routines against a fake `phone_harness` in
`tests/fake_ph`, so they need no phone.

The Desktop smoke test loads `desktop/plugin.js` under stubbed SDK modules and
checks the contributions and run-log rendering. It needs Node 20.6 or later.

## License

MIT. See [LICENSE](LICENSE).
