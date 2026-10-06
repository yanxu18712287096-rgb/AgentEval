class Stock:
    def __init__(self, quantities):
        self.quantities = dict(quantities)

    def take(self, items):
        for sku, amount in items.items():
            if sku not in self.quantities or self.quantities[sku] < amount:
                raise ValueError("insufficient stock")
            self.quantities[sku] -= amount

    def restore(self, items):
        for sku, amount in items.items():
            self.quantities[sku] += amount

    def snapshot(self):
        return dict(self.quantities)
