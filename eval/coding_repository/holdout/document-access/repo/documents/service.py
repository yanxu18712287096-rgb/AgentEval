class DocumentService:
    def __init__(self, repository, cache):
        self.repository = repository
        self.cache = cache

    def read(self, actor, doc_id):
        cached = self.cache.get(doc_id)
        if cached is not None:
            return cached
        row = self.repository.get(doc_id)
        if actor != row["owner"] and actor not in row["shared"]:
            raise PermissionError("not shared")
        result = {key: row[key] for key in ("id", "title", "body", "revision")}
        self.cache.put(doc_id, result)
        return result

    def read_many(self, actor, ids):
        return [self.read(actor, doc_id) for doc_id in ids]
