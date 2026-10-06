from datetime import datetime, timezone


def parse(value):
    return datetime.fromisoformat(value).replace(tzinfo=None)


def bounds(start, end):
    begin, finish = parse(start), parse(end)
    if begin >= finish:
        raise ValueError("empty or reversed interval")
    return begin, finish
