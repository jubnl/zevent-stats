import httpx


def fetch(url: str, timeout: float = 20.0) -> bytes:
    r = httpx.get(url, timeout=timeout, headers={"User-Agent": "zevent-tracker/0.1"})
    r.raise_for_status()
    return r.content
