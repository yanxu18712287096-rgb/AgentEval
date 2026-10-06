import copy


class Repository:
    def __init__(self, rows):
        self.rows = {row["id"]: copy.deepcopy(row) for row in rows}

    def get(self, doc_id):
        return copy.deepcopy(self.rows[doc_id])

    def update(self, doc_id, **changes):
        self.rows[doc_id].update(copy.deepcopy(changes))
        return self.get(doc_id)
