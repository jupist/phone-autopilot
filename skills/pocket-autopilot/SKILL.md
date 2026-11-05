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
