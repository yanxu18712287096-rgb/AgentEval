def build_request(config, url):
    return {
        "url": url,
        "retries": config.retries or 3,
        "cache": config.cache or True,
        "label": config.label or "default",
    }
