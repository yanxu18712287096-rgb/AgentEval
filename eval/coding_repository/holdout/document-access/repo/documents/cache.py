import copy


class Cache:
    def __init__(self):
        self.entries = {}

    def get(self, key):
        return copy.deepcopy(self.entries.get(key))

    def put(self, key, value):
        self.entries[key] = copy.deepcopy(value)
