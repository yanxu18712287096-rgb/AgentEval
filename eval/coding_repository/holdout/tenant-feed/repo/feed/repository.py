import copy


class Repository:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)

    def page(self, tenant, after, count):
        rows = sorted(self.rows, key=lambda row: row["created_at"])
        rows = [row for row in rows if after is None or row["created_at"] > after[0]]
        rows = rows[:count]
        return copy.deepcopy([row for row in rows if row["tenant"] == tenant])
