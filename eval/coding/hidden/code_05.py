from target import paginate

assert paginate(list(range(6)), 1, 2) == [0, 1]
assert paginate(list(range(6)), 2, 2) == [2, 3]
assert paginate(list(range(6)), 3, 2) == [4, 5]
