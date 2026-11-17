"""Named checkpoints inside commands. A no-op in production; tests register callables to pause a
transaction at an exact point (e.g. right after its locks are held) and force a specific interleaving."""

from collections.abc import Callable

_hooks: dict[str, Callable[[], None]] = {}


def checkpoint(name: str) -> None:
    hook = _hooks.get(name)
    if hook is not None:
        hook()


def register(name: str, fn: Callable[[], None]) -> None:
    _hooks[name] = fn


def clear() -> None:
    _hooks.clear()
