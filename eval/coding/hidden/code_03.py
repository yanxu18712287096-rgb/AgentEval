from target import parse_row

assert parse_row('one,"two,three",four') == ["one", "two,three", "four"]
assert parse_row('"one,two",three') == ["one,two", "three"]
