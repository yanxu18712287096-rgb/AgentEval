from .timeutil import bounds
from .reporting import list_active


class BookingService:
    def __init__(self, store):
        self.store = store

    def _check(self, candidate):
        start, end = bounds(candidate["start"], candidate["end"])
        for row in self.store.all():
            if row["room"] != candidate["room"]:
                continue
            old_start, old_end = bounds(row["start"], row["end"])
            if start <= old_end and old_start <= end:
                raise ValueError("conflict")

    def create(self, booking_id, room, start, end):
        if booking_id in self.store.rows:
            raise ValueError("duplicate id")
        row = dict(id=booking_id, room=room, start=start, end=end, status="active")
        self._check(row)
        self.store.put(row)
        return self.store.get(booking_id)

    def reschedule(self, booking_id, start, end):
        row = self.store.get(booking_id)
        row.update(start=start, end=end)
        self.store.put(row)
        self._check(row)
        return self.store.get(booking_id)

    def cancel(self, booking_id):
        row = self.store.get(booking_id)
        row["status"] = "cancelled"
        self.store.put(row)
        return self.store.get(booking_id)

    def list_active(self):
        return list_active(self.store)
