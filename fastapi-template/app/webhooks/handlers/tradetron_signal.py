"""DEPRECATED: Legacy module name retained for backward compatibility.

All TradeThrone signal handling now lives in
``app.webhooks.handlers.tradethrone_signal``.  This shim keeps older
imports (``handle_tradetron_signal``) working without duplicating the
worker-pool registration.
"""

from __future__ import annotations

from app.webhooks.handlers.tradethrone_signal import (  # noqa: F401
    handle_tradethrone_signal,
)

# Legacy alias — old integrations may still import this name.
handle_tradetron_signal = handle_tradethrone_signal