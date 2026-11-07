"""App guard: source prepended to phone_exec scripts and routine runs.

Runs inside phone-harness with its helpers as globals, after lines defining ALLOWED_APPS and
_GUARD_ASK. A script can reach any app (a tap on a launcher label, a notification, a deep link),
so a static check of the code cannot keep it out of apps the user has not allowed. A watchdog
thread checks the foreground app instead.

On an app the user has not allowed yet, the script pauses where it is: every phone helper the
script calls waits, so it neither acts in the app nor reads it. The guard writes asking.json,
which the plugin turns into an Allow / Not now card, and polls answers.json. Allow resumes the
script on the same screen; Not now, or no answer in time, sends the phone Home and stops it.
Android's own permission dialog is never allowed this way: the script stops at once and leaves
the dialog for the user. Only stdlib imports, same as prelude.py.
"""

import _thread
import json as _guard_json
import os as _guard_os
import threading as _guard_threading
import time as _guard_time

_GUARD_POLL_S = 0.6
_GUARD_ANSWER_POLL_S = 0.4
# Leave the runner time to report the stop before its own timeout kills the script.
_GUARD_STOP_MARGIN_S = 10.0
# System surfaces a script passes through without opening an app: the shade, recents, OS dialogs.
_GUARD_SYSTEM = {"com.android.systemui", "android"}
# Android's "Allow <app> to ...?" dialog: only the user answers it, so the guard stops and leaves it up.
_GUARD_PERMISSION_DIALOGS = {"com.google.android.permissioncontroller", "com.android.permissioncontroller"}
# An app that asks for a permission the user already denied for good still opens the dialog activity,
# which closes itself in a few hundred milliseconds without showing anything. Only a dialog still up
# after this long is one the user has to answer.
_GUARD_DIALOG_GRACE_S = 1.2

_GUARD_STARTED = _guard_time.time()
_GUARD_OPEN = _guard_threading.Event()  # set: the script may use the phone
_GUARD_OPEN.set()
_GUARD_STOPPED = [False]
# Seconds the script spent paused, so the prelude keeps the wait out of timeouts and timings.
_GUARD_PAUSED_S = [0.0]


def _guard_gate(fn):
    """Make a phone helper wait while the guard is asking the user about an app."""
    def gated(*args, **kwargs):
        if not _GUARD_OPEN.is_set() and _guard_threading.current_thread() is _guard_threading.main_thread():
            began = _guard_time.monotonic()
            while not _GUARD_OPEN.wait(0.2):
                pass
            _GUARD_PAUSED_S[0] += _guard_time.monotonic() - began
            if _GUARD_STOPPED[0]:
                raise KeyboardInterrupt
        return fn(*args, **kwargs)

    gated.__name__, gated.__doc__ = getattr(fn, "__name__", "helper"), getattr(fn, "__doc__", None)
    return gated


# Every public callable here is a phone-harness helper. The watchdog thread passes every gate.
for _guard_name, _guard_value in list(globals().items()):
    if not _guard_name.startswith("_") and callable(_guard_value) and not isinstance(_guard_value, type):
        globals()[_guard_name] = _guard_gate(_guard_value)


def _guard_home_packages():
    """Every launcher on the phone, so going Home never trips the guard."""
    try:
        out = shell("cmd package query-activities --brief -a android.intent.action.MAIN "
                    "-c android.intent.category.HOME")
    except Exception:
        return set()
    # Settings registers a FallbackHome for boot; it is not a launcher and must not whitelist Settings.
    return {line.strip().split("/")[0] for line in str(out).splitlines()
            if "/" in line and "FallbackHome" not in line}


def _guard_allowed():
    return set(ALLOWED_APPS) | _GUARD_SYSTEM | _guard_home_packages()


def _guard_event(**fields):
    print("@@AUTOPILOT " + _guard_json.dumps(fields), flush=True)


def _guard_write(path, data):
    tmp = f"{path}.{_guard_os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(_guard_json.dumps(data))
    _guard_os.replace(tmp, path)


def _guard_answer(pkg, asked_at):
    """'allowed', 'denied', or None while the user has not answered since the question appeared."""
    try:
        with open(_GUARD_ASK["answers"], encoding="utf-8") as handle:
            answer = _guard_json.load(handle).get(pkg) or {}
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(answer, dict) or answer.get("at", 0) < asked_at:
        return None
    return answer.get("answer") if answer.get("answer") in ("allowed", "denied") else None


def _guard_ask(pkg):
    """Pause the script until the user answers: 'allowed', 'denied', or None for no answer in time."""
    wait_s = min(_GUARD_ASK["wait_s"],
                 _GUARD_STARTED + _GUARD_ASK["script_timeout_s"] - _GUARD_STOP_MARGIN_S - _guard_time.time())
    if wait_s <= 0:
        return None
    _GUARD_OPEN.clear()
    asked_at = _guard_time.time()
    try:
        _guard_write(_GUARD_ASK["asking"], {"app": pkg, "source": _GUARD_ASK["source"],
                                            "routine": _GUARD_ASK["routine"], "at": asked_at})
    except OSError:
        return None
    _guard_event(event="asking", app=pkg)
    print(f"phone-autopilot: paused. {pkg} is not an allowed app yet; waiting for the user to answer "
          f"Allow or Not now in the phone pane (or type /routines allow {pkg}).", flush=True)
    try:
        while _guard_time.time() - asked_at < wait_s:
            answer = _guard_answer(pkg, asked_at)
            if answer:
                return answer
            _guard_time.sleep(_GUARD_ANSWER_POLL_S)
        return None
    finally:
        try:
            _guard_os.remove(_GUARD_ASK["asking"])
        except OSError:
            pass


def _guard_stop(pkg, reason):
    """reason: 'dialog' (Android permission dialog), 'denied' (Not now) or 'no_answer'."""
    dialog = reason == "dialog"
    _guard_event(event="guard", status="needs_user" if dialog else "blocked", app=pkg, reason=reason)
    if dialog:
        print("phone-autopilot: stopped. Android is asking for a permission; the user must answer it "
              "on the phone. Never tap it from a script.", flush=True)
    else:
        if reason == "denied":
            print(f"phone-autopilot: stopped. The user chose Not now for {pkg}; do not try that app again "
                  f"unless they ask.", flush=True)
        else:
            print(f"phone-autopilot: stopped. Nobody answered whether {pkg} may be used. The user can "
                  f"allow it with Allow in the phone pane, or by typing /routines allow {pkg}", flush=True)
        try:
            send("nav.home")
        except Exception:
            pass
    _GUARD_STOPPED[0] = True
    _thread.interrupt_main()
    _GUARD_OPEN.set()


def _guard_foreground():
    try:
        return str(current_app() or "")
    except Exception:
        return ""


def _guard_dialog_stays(pkg):
    """True when a permission dialog is still in front after the grace period: a real question."""
    deadline = _guard_time.time() + _GUARD_DIALOG_GRACE_S
    while _guard_time.time() < deadline:
        _guard_time.sleep(_GUARD_POLL_S / 3)
        if _guard_foreground() != pkg:
            return False
    return True


def _guard_watch(allowed):
    while True:
        pkg = _guard_foreground()
        if pkg and pkg not in allowed:
            if pkg in _GUARD_PERMISSION_DIALOGS:
                if _guard_dialog_stays(pkg):
                    _guard_stop(pkg, "dialog")
                    return
            else:
                answer = _guard_ask(pkg)
                if answer != "allowed":
                    _guard_stop(pkg, answer or "no_answer")
                    return
                allowed.add(pkg)
                _guard_event(event="allowed", app=pkg)
                _GUARD_OPEN.set()
        _guard_time.sleep(_GUARD_POLL_S)


if globals().get("__name__") == "__main__":  # in a script, not when a test loads the functions
    _guard_threading.Thread(target=_guard_watch, args=(_guard_allowed(),), daemon=True).start()
