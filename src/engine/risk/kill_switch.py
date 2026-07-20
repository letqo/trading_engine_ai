"""Remote-friendly kill switch.

Two trigger mechanisms, checked every loop iteration by the live worker:
1. HALT=true environment variable (flip it in Railway and redeploy/restart).
2. A HALT flag file in the working directory (local CLI: `engine kill`).

Either one being present is sufficient; there is deliberately no single
point of failure between "operator wants to stop" and "trading stops".
"""

from __future__ import annotations

from engine.config.settings import Settings


def is_kill_switch_engaged(settings: Settings) -> bool:
    if settings.halt:
        return True
    return settings.halt_file.exists()


def engage_kill_switch(settings: Settings) -> None:
    settings.halt_file.write_text("halted via CLI kill switch\n")


def disengage_kill_switch(settings: Settings) -> None:
    if settings.halt_file.exists():
        settings.halt_file.unlink()
