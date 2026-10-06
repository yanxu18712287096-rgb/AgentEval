from client.service import prepare

assert prepare("/items") == {"url": "/items", "retries": 3, "cache": True, "label": "default"}
assert prepare("/items", cli={"retries": 5})["retries"] == 5
assert prepare("/items", env={"APP_LABEL": "batch"})["label"] == "batch"
print("public client checks passed")
