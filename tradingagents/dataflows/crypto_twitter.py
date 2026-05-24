"""Crypto Twitter vendor — placeholder pending data-source decision.

The Twitter data source is unresolved in
``docs/dataflows-decisions.md`` §5 (Nitter / RSSHub / paid Twitter API
/ third-party aggregator). To unblock the Analyst pipeline this module
exposes the public signature and returns a degraded ``Response``
(``ok=False``) so prompts read "Twitter data pending" rather than
crashing. Implementation slots in by populating ``_get_tweets``.

Contract is stable: any future implementation must return
``Response[SocialPost]`` with ``platform == "twitter"``.
"""

from __future__ import annotations

import time

from .crypto_types import Meta, Response, SocialPost


_NOTE = ("Twitter data source pending — see docs/dataflows-decisions.md §5. "
         "Sentiment Analyst should treat tweets section as unavailable.")


def _get_tweets(symbols: tuple[str, ...],
                *, hours: int = 24,
                min_engagement: int | None = None) -> Response[SocialPost]:
    return Response(data=[], meta=Meta(
        fetched_at=int(time.time() * 1000),
        vendor="twitter",
        ok=False,
        note=_NOTE,
    ))


def get_tweets(symbols: tuple[str, ...], hours: int = 24,
               min_engagement: int | None = None) -> str:
    sym_label = ",".join(s.upper() for s in symbols)
    return (
        f"# Crypto Twitter — symbols: {sym_label} · last {hours}h\n"
        f"# Posts: 0\n"
        f"# ok=False · note: {_NOTE}\n"
        f"<no Twitter data available>"
    )
