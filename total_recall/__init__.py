"""total-recall — Click CLI for ops, debugging, and ad-hoc queries.

This package wraps the :mod:`index` ingest/query layer with a stable shell
surface that is also called from the plugin hooks (e.g. WT-7's
``stop-index.sh`` invokes ``python3 -m total_recall index
--since-last-tick``).

Heavy imports (sqlite, fastembed, the extractor modules) live inside each
command function so ``total-recall --help`` stays fast and works even when
optional dependencies are missing.
"""

from __future__ import annotations

__version__ = "1.5.0"

__all__ = ["__version__"]
