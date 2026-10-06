from inventory.stock import Stock
from inventory.ledger import Ledger
from inventory.service import Reservations

stock = Stock({"a": 8, "b": 5})
service = Reservations(stock, Ledger())
assert service.reserve("one", {"a": 2, "b": 1})["status"] == "active"
assert stock.snapshot() == {"a": 6, "b": 4}
assert service.release("one")["status"] == "released"
assert stock.snapshot() == {"a": 8, "b": 5}
print("public inventory checks passed")
