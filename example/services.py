"""Domain logic for the Flask example.

Functions here are the candidates that probes target. Kept deliberately
plain so the qualnames (``services.get_user`` etc., since the app runs
with the example dir on ``sys.path`` and imports this module as
``services``) line up with what a hogtrace probe specifier would name.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


# Toy in-memory stores. Reset when the process restarts.
_USERS: Dict[int, Dict[str, Any]] = {
    1: {"id": 1, "name": "Ada", "email": "ada@example.com"},
    2: {"id": 2, "name": "Lin", "email": "lin@example.com"},
}
_ORDERS: List[Dict[str, Any]] = [
    {"id": 1001, "user_id": 1, "item": "keyboard", "qty": 1},
    {"id": 1002, "user_id": 1, "item": "mouse", "qty": 2},
    {"id": 1003, "user_id": 2, "item": "monitor", "qty": 1},
]
_NEXT_USER_ID = 3
_NEXT_ORDER_ID = 1004


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Look up a user by ID."""
    return _USERS.get(user_id)


def create_user(name: str, email: str) -> Dict[str, Any]:
    """Create a new user. Validates non-empty name + email."""
    global _NEXT_USER_ID
    if not name or not email:
        raise ValueError("name and email are required")
    user = {"id": _NEXT_USER_ID, "name": name, "email": email}
    _USERS[_NEXT_USER_ID] = user
    _NEXT_USER_ID += 1
    return user


def list_orders_for_user(user_id: int) -> List[Dict[str, Any]]:
    """All orders belonging to ``user_id``. Empty list if none."""
    return [o for o in _ORDERS if o["user_id"] == user_id]


def create_order(user_id: int, item: str, qty: int) -> Dict[str, Any]:
    """Create an order. Raises ``LookupError`` if the user doesn't exist."""
    global _NEXT_ORDER_ID
    if user_id not in _USERS:
        raise LookupError(f"user {user_id} not found")
    if qty <= 0:
        raise ValueError("qty must be positive")
    order = {"id": _NEXT_ORDER_ID, "user_id": user_id, "item": item, "qty": qty}
    _ORDERS.append(order)
    _NEXT_ORDER_ID += 1
    return order


def slow_compute(n: int) -> int:
    """A deliberately slow function so we can poke at timing probes."""
    total = 0
    for i in range(n):
        total += i * i
    time.sleep(0.01)
    return total
