def recover(store, now):
    """Recover expired running work before a worker starts polling."""
    count = 0
    for job in store.jobs.values():
        if job["status"] == "running" and job["lease_until"] < now:
            job["status"] = "pending"
            job["lease_until"] = None
            count += 1
    return count
