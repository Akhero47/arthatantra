import hashlib

from transfer_request_hash import hash_transfer_request


def test_same_request_produces_same_hash() -> None:
    expected = hashlib.sha256(b"[1,2,30]").hexdigest()
    assert hash_transfer_request(1, 2, 30) == hash_transfer_request(1, 2, 30) == expected


def test_different_amount_produces_different_hash() -> None:
    assert hash_transfer_request(1, 2, 30) != hash_transfer_request(1, 2, 31)


def test_different_source_produces_different_hash() -> None:
    assert hash_transfer_request(1, 2, 30) != hash_transfer_request(3, 2, 30)


def test_different_destination_produces_different_hash() -> None:
    assert hash_transfer_request(1, 2, 30) != hash_transfer_request(1, 3, 30)