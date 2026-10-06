import copy


class Ledger:
    def __init__(self):
        self.records = {}

    def get(self, order_id):
        return copy.deepcopy(self.records[order_id])

    def add(self, order_id, items):
        self.records[order_id] = {"order_id": order_id, "items": copy.deepcopy(items), "status": "active"}
        return self.get(order_id)

    def mark_released(self, order_id):
        self.records[order_id]["status"] = "released"
