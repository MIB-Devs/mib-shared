"""`visitor_ip` (mib-infra#46): the header's address, or None, never the peer."""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from mib_shared import VISITOR_IP_HEADER, visitor_ip

app = FastAPI()


@app.get("/ip")
def ip(request: Request) -> dict:
    return {"ip": visitor_ip(request)}


client = TestClient(app)


def get(headers: dict) -> str | None:
    return client.get("/ip", headers=headers).json()["ip"]


def test_the_cloudflare_header_is_the_visitor():
    assert get({VISITOR_IP_HEADER: "203.0.113.7"}) == "203.0.113.7"


def test_an_ipv6_address_is_normalised():
    assert get({VISITOR_IP_HEADER: "2001:DB8:0:0::1"}) == "2001:db8::1"


def test_surrounding_whitespace_is_ignored():
    assert get({VISITOR_IP_HEADER: " 203.0.113.7 "}) == "203.0.113.7"


def test_no_header_is_unknown_not_the_peer_address():
    # TestClient's peer is "testclient"; production's is the gateway.
    assert get({}) is None


@pytest.mark.parametrize(
    "value", ["", "unknown", "203.0.113.7, 198.51.100.1", "999.1.1.1", "evil; DROP"]
)
def test_a_value_that_is_not_one_address_is_unknown(value):
    assert get({VISITOR_IP_HEADER: value}) is None
