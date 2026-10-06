from booking.store import Store
from booking.service import BookingService

service = BookingService(Store())
service.create("a", "room", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
service.create("b", "other", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
assert [row["id"] for row in service.list_active()] == ["a", "b"]
assert service.cancel("a")["status"] == "cancelled"
assert [row["id"] for row in service.list_active()] == ["b"]
print("public booking checks passed")
