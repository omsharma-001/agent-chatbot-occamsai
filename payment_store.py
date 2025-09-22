# payment_store.py
from typing import Dict, Any

class PaymentStore:
    """
    Super-lightweight in-memory holder for payment session status.
    Replace with DB/Redis if you need persistence across restarts.
    """
    def __init__(self) -> None:
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def set(self, key: str, data: Dict[str, Any]) -> None:
        self._sessions[key] = data

    def get(self, key: str) -> Dict[str, Any]:
        return self._sessions.get(key, {})

    def update(self, key: str, **patch: Any) -> None:
        current = self._sessions.get(key, {})
        current.update(patch)
        self._sessions[key] = current
