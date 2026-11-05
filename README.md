# Pocket Autopilot

Teach your phone a task once, then replay it without the model.

Pocket Autopilot is a Hermes plugin for phone routines. The agent explores a
task on your phone with `phone_exec`, and once it works, saves the steps as a
**routine**. Routines replay with no LLM in the loop, verify themselves with
**checkpoints** (a text or app check after every screen change), and ask the
agent to heal them when an app changes. A routine can run every day through the
`daily-routine` automation blueprint. The Desktop pane shows the live phone,
your routines and a step-by-step run log.

## Requirements

- Hermes Agent `>=0.21.6`
- The `phone-harness` plugin (installed automatically as a dependency)
- Android with USB debugging enabled and `adb` on your PATH, **or** an iPhone
  through iPhone Mirroring on macOS
- Optional: [`scrcpy`](https://github.com/Genymobile/scrcpy) for the pop-out window

## Install

```bash
hermes plugins install <repo-url>
```

## Quick start

Plug in your phone, then talk to Hermes in a chat:

> Open the shop app and claim today's reward. Save it as a routine called `daily-claim`.

The agent drives the phone with `phone_exec` until the task works, then calls
the `routine` tool to save the script. You approve the save, and the routine
is ready to run:

> Run `daily-claim`.

Routines use `PARAMS` for values that change between runs, and every screen
transition has a checkpoint so a failure stops at the right place with a
screenshot.

To run a routine every morning, add the blueprint:

```
/blueprint pocket-autopilot:daily-routine
```

Other commands: `/routines` lists routines and their last run, and
`/routines show <name>` prints the code and recent runs.

## How approval works

- **Saving** a routine, and **running** one that has not been approved yet,
  goes through the normal Hermes approval prompt. The prompt shows the title,
  the routine name, and the first lines of the code. Nothing new runs on your
  phone without your yes.
- Once approved, a routine runs without prompts. Editing it (a new version)
  requires approval again.
- **Scheduled runs never self-repair.** If a checkpoint misses unattended, the
  run stops, keeps the failure screenshot, and reports which checkpoint failed.
  Reply `fix <routine>` in a chat where you can approve the repair.

## Desktop pane

With Hermes Desktop, a **Pocket Autopilot** pane sits on the right:

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

- Routines are Python code written by the model and run on **this computer**
  through `phone-harness`, which taps and types on your **real phone**.
- Screenshots taken during runs and the live view are stored under the plugin's
  data directory in `HERMES_HOME` (`plugin-data/pocket-autopilot/`).
- The plugin makes **no network calls of its own** and has **no telemetry**.
  Any network use comes from Hermes or the model provider you configured.
- scrcpy is launched only when you click Pop out, and only if it is installed.
- Run environments do not receive variables whose names end in `_KEY`,
  `_TOKEN`, `_SECRET` or `_PASSWORD`.

## How it works

```
 chat / cron ──► agent ──► routine tool ──► approval prompt (Hermes)
                              │
                              ▼
                    engine: prelude + routine.py
                              │  subprocess
                              ▼
                 phone_harness.run ──► phone (adb / iPhone Mirroring)
                              │
              @@POCKET events on stdout
                              │
          ┌───────────────────┴───────────────────┐
          ▼                                       ▼
  run record on disk                 gateway event stream
  (plugin-data/runs/…)               plugin.pocket-autopilot.run.*
                                                  │
                                                  ▼
                                    Desktop pane, chip, transcript card

 Desktop pane ──► dashboard API (/api/plugins/pocket-autopilot)
                  /status /frame /tap /key /routines /runs …
```

## Development

```bash
node tests/js/run_check.mjs
```

The Desktop smoke test loads `desktop/plugin.js` under stubbed SDK modules and
checks the contributions and run-log rendering. It needs Node 20.6 or later.

## License

MIT. See [LICENSE](LICENSE).
