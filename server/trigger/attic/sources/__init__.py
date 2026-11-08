"""Pull-based source adapters for mowu-trigger.

Every module here exposes a ``BaseSource`` subclass and a module-level
``SOURCE`` alias. ``server.py`` imports ``sources.<name>`` where ``<name>`` is
the key in ``sources.json``; ``email`` maps to ``email_imap.py``.

To add a source: drop ``sources/mysource.py`` here, subclass ``BaseSource``,
call ``self.emit(...)`` for each event, and enable it in ``sources.json``.
"""

from .base import BaseSource

__all__ = ["BaseSource"]
