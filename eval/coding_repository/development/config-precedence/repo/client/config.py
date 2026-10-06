from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    retries: int
    cache: bool
    label: str


def parse_env(env):
    result = {}
    if "APP_RETRIES" in env:
        result["retries"] = int(env["APP_RETRIES"])
    if "APP_CACHE" in env:
        result["cache"] = bool(env["APP_CACHE"])
    if "APP_LABEL" in env:
        result["label"] = env["APP_LABEL"]
    return result


def resolve(defaults, file_config, env, cli):
    merged = dict(defaults)
    for layer in (file_config, parse_env(env), cli):
        for key, value in layer.items():
            if value:
                merged[key] = value
    return Config(**merged)
