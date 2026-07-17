from python.service import process_order


def test_process_order_normalizes_identifier() -> None:
    assert process_order(" ORDER-7 ")["id"] == "order-7"
