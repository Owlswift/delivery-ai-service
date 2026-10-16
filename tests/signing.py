import json
import time

from app.security.auth import sign_request

SECRETS = {"acme": "acme-secret", "beta": "beta-secret"}


def signed(client, method, path, payload=None, provider="acme", secret=None, ts=None, tamper=False):
    body = json.dumps(payload).encode() if payload is not None else b""
    timestamp = str(ts if ts is not None else time.time())
    signature = sign_request(secret or SECRETS.get(provider, "x"), timestamp, method, path, body)
    if tamper:
        body = body + b" "
    headers = {
        "X-Provider-Id": provider,
        "X-Timestamp": timestamp,
        "X-Signature": signature,
        "Content-Type": "application/json",
    }
    return client.request(method, path, content=body, headers=headers)
