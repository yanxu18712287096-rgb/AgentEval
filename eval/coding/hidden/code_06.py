from target import lookup

words = {"en": {"hello": "Hello"}, "fr": {"hello": "Bonjour"}}
assert lookup("hello", "en", words) == "Hello"
assert lookup("hello", "fr", words) == "Bonjour"
