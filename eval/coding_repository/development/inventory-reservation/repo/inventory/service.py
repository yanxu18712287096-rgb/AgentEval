class Reservations:
    def __init__(self, stock, ledger):
        self.stock = stock
        self.ledger = ledger

    def reserve(self, order_id, items):
        self.stock.take(items)
        return self.ledger.add(order_id, items)

    def release(self, order_id):
        record = self.ledger.get(order_id)
        self.stock.restore(record["items"])
        self.ledger.mark_released(order_id)
        return self.ledger.get(order_id)
