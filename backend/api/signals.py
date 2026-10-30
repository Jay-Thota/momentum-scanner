"""
backend/api/signals.py

Decodes StockSignals rows into the signals dict the API returns.

Scans before the layer 1 signal C fix stored numpy bools through
json.dumps(default=str), so they read back as the strings "True"/"False".
load_signals turns those back into booleans, so the API returns real booleans
whichever scan wrote the row. No layer emits "True" or "False" as a genuine
string signal.
"""

import json
from typing import Any

from backend.db.models import StockSignals

_BOOL_STRINGS = {"True": True, "False": False}


def _restore_bools(value: Any) -> Any:
    if isinstance(value, str):
        return _BOOL_STRINGS.get(value, value)
    if isinstance(value, dict):
        return {k: _restore_bools(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_restore_bools(v) for v in value]
    return value


def load_signals(signal_rows: list[StockSignals]) -> dict[str, Any]:
    return {s.signal_key: _restore_bools(json.loads(s.signal_value)) if s.signal_value else None
            for s in signal_rows}
