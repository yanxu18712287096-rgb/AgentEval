from .cursor import encode, decode


def feed(repo, tenant, limit, cursor=None):
    after = decode(cursor, tenant) if cursor is not None else None
    rows = repo.page(tenant, after, limit + 1)
    items = rows[:limit]
    next_cursor = encode(tenant, items[-1]) if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}
