from .recovery import recover


def run_one(store, job_id, handler, now, lease_seconds=30):
    recover(store, now)
    claimed = store.claim(job_id, now, lease_seconds)
    if claimed is None:
        return False
    try:
        handler(claimed["payload"])
    except RuntimeError:
        store.finish(job_id, False)
    else:
        store.finish(job_id, True)
    return True
