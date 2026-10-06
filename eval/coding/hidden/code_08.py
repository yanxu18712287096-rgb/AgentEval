from target import request_with_retry

calls = []
def fail():
    calls.append(1)
    raise RuntimeError("temporary")

try:
    request_with_retry(fail, 2)
except RuntimeError:
    pass
else:
    raise AssertionError("expected failure")
assert len(calls) == 2
