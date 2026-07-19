"""Synthetic order service used only by the public benchmark corpus."""


def normalize_order(order_id: str) -> str:
    return order_id.strip().lower()


def load_order(order_id: str) -> dict[str, str]:
    return {"id": order_id, "state": "ready"}


def process_order(order_id: str) -> dict[str, str]:
    normalized = normalize_order(order_id)
    return load_order(normalized)
