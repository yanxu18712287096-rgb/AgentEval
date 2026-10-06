import base64
import json


def encode(tenant, row):
    value = {"time": row["created_at"]}
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


def decode(token, tenant):
    try:
        value = json.loads(base64.urlsafe_b64decode(token).decode())
        return value["time"], 0
    except Exception as exc:
        raise ValueError("invalid cursor") from exc
