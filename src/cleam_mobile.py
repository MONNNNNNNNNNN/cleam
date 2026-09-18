"""Entry module for `flet build apk` / `flet build ipa`.

Flet's mobile build imports this module and expects the app to start at import
time. Everything a phone is allowed to do is in the same GUI; see
docs/platforms.md for how little that is.
"""
from cleam.gui import run

run()
