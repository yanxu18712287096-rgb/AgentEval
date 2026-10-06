import copy


class Store:
    def __init__(self):
        self.rows = {}

    def get(self, booking_id):
        return copy.deepcopy(self.rows[booking_id])

    def put(self, row):
        self.rows[row["id"]] = copy.deepcopy(row)

    def all(self):
        return copy.deepcopy(list(self.rows.values()))
