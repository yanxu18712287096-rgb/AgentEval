from target import get_feature_flag

assert get_feature_flag({"x": False}, "x", True) is False
assert get_feature_flag({}, "x", True) is True
