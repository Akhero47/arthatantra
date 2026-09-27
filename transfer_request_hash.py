import hashlib
import json


def hash_transfer_request(source_account_id: int, destination_account_id: int, amount: int) -> str:
    request = json.dumps([source_account_id, destination_account_id, amount], separators=(",", ":"))
    return hashlib.sha256(request.encode("utf-8")).hexdigest()