"""Feature switches (mib-infra#107)."""
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from mib_shared import install_error_handlers
from mib_shared.features import (
    ADMIN_FEATURES,
    FEATURES,
    USER_FEATURES,
    UnknownFeatureError,
    feature_enabled,
    parse_features_off,
    require_feature,
)


def test_every_name_is_dotted_by_surface():
    assert ADMIN_FEATURES and USER_FEATURES
    assert all(n.startswith("admin.") for n in ADMIN_FEATURES)
    assert all(n.startswith("user.") for n in USER_FEATURES)
    assert FEATURES == ADMIN_FEATURES | USER_FEATURES


@pytest.mark.parametrize("raw", [None, "", "  ", " , ,"])
def test_empty_means_everything_on(raw):
    assert parse_features_off(raw) == frozenset()


def test_names_are_trimmed_and_deduplicated():
    parsed = parse_features_off(" admin.tax , user.signup,admin.tax,")
    assert parsed == {"admin.tax", "user.signup"}


def test_an_iterable_is_read_like_the_string():
    assert parse_features_off(["admin.tax", " user.signup "]) == {"admin.tax", "user.signup"}


def test_unknown_names_fail_and_are_all_named():
    with pytest.raises(UnknownFeatureError) as raised:
        parse_features_off("admin.tax,admin.taxx,user.chat")
    message = str(raised.value)
    assert "admin.taxx" in message and "user.chat" in message
    assert "user.ai_chat" in message


def test_the_surface_prefix_alone_is_not_a_name():
    with pytest.raises(UnknownFeatureError):
        parse_features_off("admin")


def test_feature_enabled():
    off = frozenset({"admin.tax"})
    assert not feature_enabled("admin.tax", off)
    assert feature_enabled("admin.plans", off)


def test_feature_enabled_rejects_a_misspelt_name():
    with pytest.raises(UnknownFeatureError):
        feature_enabled("admin.taxx", frozenset())


def test_require_feature_rejects_a_misspelt_name_when_declared():
    with pytest.raises(UnknownFeatureError):
        require_feature("admin.taxx", lambda: frozenset())


def _app(off: dict[str, frozenset[str]]) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    switch = require_feature("admin.tax", lambda: off["now"])

    @app.post("/admin/tax/ppn", dependencies=[Depends(switch)])
    async def write() -> dict:
        return {"saved": True}

    @app.get("/internal/tax/ppn")
    async def read() -> dict:
        return {"rate": "11"}

    return app


def test_a_switched_off_route_answers_404_in_the_envelope():
    client = TestClient(_app({"now": frozenset({"admin.tax"})}))
    response = client.post("/admin/tax/ppn")
    assert response.status_code == 404
    assert response.json()["error_code"] == "not_found"


def test_the_route_is_untouched_while_on():
    client = TestClient(_app({"now": frozenset()}))
    assert client.post("/admin/tax/ppn").json() == {"saved": True}


def test_another_feature_off_leaves_the_route_on():
    client = TestClient(_app({"now": frozenset({"admin.plans"})}))
    assert client.post("/admin/tax/ppn").status_code == 200


def test_an_unguarded_read_stays_on_while_the_write_is_off():
    client = TestClient(_app({"now": frozenset({"admin.tax"})}))
    assert client.get("/internal/tax/ppn").json() == {"rate": "11"}


def test_the_setting_is_read_per_request():
    state = {"now": frozenset()}
    client = TestClient(_app(state))
    assert client.post("/admin/tax/ppn").status_code == 200
    state["now"] = frozenset({"admin.tax"})
    assert client.post("/admin/tax/ppn").status_code == 404
