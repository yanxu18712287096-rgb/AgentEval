from .timeutil import parse


def list_active(store):
    rows = [row for row in store.all() if row["status"] == "active"]
    return sorted(rows, key=lambda row: (parse(row["start"]), row["id"]))
