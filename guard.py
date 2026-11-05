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
    print("@@POCKET " + _guard_json.dumps({"event": "guard", "status": "blocked", "app": pkg}), flush=True)
    try:
        send("nav.home")
    except Exception:
        pass
    _thread.interrupt_main()


def _guard_watch(allowed):
    while True:
        try:
            pkg = str(current_app() or "")
        except Exception:
            pkg = ""
        if pkg and pkg not in allowed:
            _guard_violation(pkg)
            return
        _guard_time.sleep(_GUARD_POLL_S)


if ALLOWED_APPS:
    _guard_threading.Thread(target=_guard_watch, args=(_guard_allowed(),), daemon=True).start()
