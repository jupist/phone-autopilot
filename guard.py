"""App guard: source prepended to phone_exec scripts and routine runs when an allowlist is set.

Runs inside phone-harness with its helpers as globals, after a line defining ALLOWED_APPS.
A script can reach any app (a tap on a launcher label, a notification, a deep link), so a
static check of the code cannot keep it out of apps the user ruled off-limits. A watchdog
thread checks the foreground app instead: on a disallowed app it presses Home and stops the
script. Only stdlib imports, same as prelude.py.
"""

import _thread
import json as _guard_json
import threading as _guard_threading
import time as _guard_time

_GUARD_POLL_S = 0.6
# System surfaces a script passes through without opening an app: the shade, recents, OS dialogs.
_GUARD_SYSTEM = {"com.android.systemui", "android"}
# Android's "Allow <app> to ...?" dialog: only the user answers it, so the guard stops and leaves it up.
_GUARD_PERMISSION_DIALOGS = {"com.google.android.permissioncontroller", "com.android.permissioncontroller"}
# An app that asks for a permission the user already denied for good still opens the dialog activity,
# which closes itself in a few hundred milliseconds without showing anything. Only a dialog still up
# after this long is one the user has to answer.
_GUARD_DIALOG_GRACE_S = 1.2


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


def _guard_violation(pkg):
    dialog = pkg in _GUARD_PERMISSION_DIALOGS
    status = "needs_user" if dialog else "blocked"
    print("@@AUTOPILOT " + _guard_json.dumps({"event": "guard", "status": status, "app": pkg}), flush=True)
    if dialog:
        print("phone-autopilot: stopped. Android is asking for a permission; the user must answer it "
              "on the phone. Never tap it from a script.", flush=True)
    else:
        print(f"phone-autopilot: stopped. {pkg} is not an allowed app. Ask the user to allow it with "
              f"Allow in the phone pane, or by typing /routines allow {pkg}", flush=True)
        try:
            send("nav.home")
        except Exception:
            pass
    _thread.interrupt_main()


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
            if pkg not in _GUARD_PERMISSION_DIALOGS or _guard_dialog_stays(pkg):
                _guard_violation(pkg)
                return
        _guard_time.sleep(_GUARD_POLL_S)


if ALLOWED_APPS:
    _guard_threading.Thread(target=_guard_watch, args=(_guard_allowed(),), daemon=True).start()
