from target import normalize_tag

assert normalize_tag("  Red   Apple  ") == "red-apple"
assert normalize_tag("A\tB") == "a-b"
