"""Offline tests for the Tier C order path. No network, no live post, no funded key.

The private key below is the throwaway
``0x0123456789012345678901234567890123456789012345678901234567890123`` — a public test
vector (address 0x1479…9325), obviously fake and never funded. It exists so the EIP-712
signature is a fixed, checkable artefact.

Determinism note: ``py_order_utils.utils.generate_seed`` salts every order with
``time * random()``, so two signings of the same spec differ by design. The golden test
pins the salt (``_fixed_salt``) and asserts the remaining pipeline — struct hash and
ECDSA signature — is byte-identical, which it is because ``eth_account.Account._sign_hash``
is RFC-6979 deterministic. Golden values were computed once against py-clob-client 0.34.6 /
py-order-utils and are inlined here; a library change that moves the struct layout, the
domain, or the amount rounding will break them, which is the point.

Everything the client would send over HTTP is stubbed: ``py_clob_client.client.get`` /
``post`` / ``delete`` are replaced, so a network call in a code path under test surfaces as
an assertion error rather than a live request.
"""
from __future__ import annotations

import json

import pytest

import py_clob_client.client as clob_client_mod
import py_order_utils.builders.order_builder as ob_mod
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderType, PostOrdersArgs
from py_clob_client.utilities import order_to_json

from backend.exec.clob_auth import MissingCredentials, load_clob_env
from backend.exec.order_client import (
    STRICT_DEFAULT,
    DryRunGuard,
    GuardRailViolation,
    OrderClient,
    OrderSpec,
    decode_signed,
)

KEY = "0x0123456789012345678901234567890123456789012345678901234567890123"
ADDRESS = "0x14791697260E4c9A71f18484C9f997B308e59325"
TOKEN = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
TOKEN_B = "11015470973432840429958794053366238695537386880771827248001019561848843035491"
GOLDEN_SALT = 479249751

GOLDEN_STRUCT_HASH = "0x9b43fab8818e26a50b0123f99ea284a39f78640f90ada036c1b3681d3499c6c5"
GOLDEN_SIGNATURE = (
    "0x0310d7dafa8e5ff7ed0fc6ef8763851d9fb2b919317ffaa18e97c8d2164f7b52"
    "5e6022430b03db802561d26e73a3c8be57de45131ef00c1c2f42985f6fa672221c"
)
GOLDEN_ORDER = {
    "salt": GOLDEN_SALT,
    "maker": ADDRESS,
    "signer": ADDRESS,
    "taker": "0x0000000000000000000000000000000000000000",
    "tokenId": TOKEN,
    "makerAmount": "50000",      # 5 shares x $0.01, 6dp fixed point
    "takerAmount": "5000000",    # 5 shares, 6dp fixed point
    "expiration": "0",
    "nonce": "0",
    "feeRateBps": "0",
    "side": "BUY",
    "signatureType": 0,
    "signature": GOLDEN_SIGNATURE,
}

# Book used by the guard tests: best bid 0.30, best ask 0.32. A $0.01-0.02 BUY is far under
# half the bid, so the rails pass unless the test is deliberately violating one.
DEFAULT_BOOK = {"bids": [{"price": "0.28", "size": "500"}, {"price": "0.30", "size": "100"}],
                "asks": [{"price": "0.32", "size": "100"}]}


# --------------------------------------------------------------------------- fixtures
@pytest.fixture
def fixed_salt(monkeypatch):
    """Pin the order salt. ``salt_generator`` is a default argument bound at def time, so
    patching the module-level ``generate_seed`` would not take — the __init__ is wrapped."""
    original = ob_mod.OrderBuilder.__init__

    def _fixed(self, exchange_address, chain_id, signer, salt_generator=None):
        original(self, exchange_address, chain_id, signer, salt_generator=lambda: GOLDEN_SALT)

    monkeypatch.setattr(ob_mod.OrderBuilder, "__init__", _fixed)


class FakeHTTP:
    """Records every request the client makes and answers from canned data."""

    def __init__(self, book=None):
        self.book = book if book is not None else DEFAULT_BOOK
        self.posts: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.post_response = {"success": True, "orderID": "0xabc", "status": "live"}
        self.orders_response = None

    def get(self, url, headers=None, data=None):
        if "/tick-size" in url:
            return {"minimum_tick_size": "0.01"}
        if "/neg-risk" in url:
            return {"neg_risk": True}
        if "/fee-rate" in url:
            return {"base_fee": 0}
        if "/book" in url:
            # Full GET /book shape — parse_raw_orderbook_summary indexes every key.
            return dict(self.book, market="0xmarket", asset_id=url.split("token_id=")[-1],
                        timestamp="0", last_trade_price="0.30", min_order_size="5",
                        neg_risk=True, tick_size="0.01", hash="0x0")
        if "/data/orders" in url:
            return {"next_cursor": "LTE=", "data": []}
        raise AssertionError(f"unexpected GET in an offline test: {url}")

    def post(self, url, headers=None, data=None):
        self.posts.append((url, data))
        if url.endswith("/orders"):
            n = len(json.loads(data))
            return self.orders_response if self.orders_response is not None else [
                dict(self.post_response, orderID=f"0x{i}") for i in range(n)
            ]
        return self.post_response

    def delete(self, url, headers=None, data=None):
        self.deletes.append(url)
        return {"canceled": ["0x0", "0x1"], "not_canceled": {}}


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(clob_client_mod, "get", fake.get)
    monkeypatch.setattr(clob_client_mod, "post", fake.post)
    monkeypatch.setattr(clob_client_mod, "delete", fake.delete)
    return fake


def make_client(guard: DryRunGuard = STRICT_DEFAULT) -> OrderClient:
    """L2 client with stub creds — enough to pass ``assert_level_2_auth`` offline."""
    raw = ClobClient(
        "https://clob.polymarket.com", chain_id=137, key=KEY,
        creds=ApiCreds(api_key="api-key-uuid", api_secret="c2VjcmV0", api_passphrase="pass"),
        signature_type=0, funder=ADDRESS,
    )
    return OrderClient(raw, guard=guard)


def spec(price=0.01, size=5.0, side="BUY", token=TOKEN) -> OrderSpec:
    return OrderSpec(token_id=token, side=side, price=price, size=size)


# --------------------------------------------------------------------------- (a) signing
def test_signing_is_deterministic_and_matches_golden(fixed_salt, http):
    oc = make_client()
    signed_a, ms_a = oc.build_signed(spec(), "FOK")
    signed_b, ms_b = oc.build_signed(spec(), "FOK")

    assert signed_a.dict() == GOLDEN_ORDER
    assert signed_a.dict() == signed_b.dict()          # byte-identical across two builds
    assert signed_a.signature == GOLDEN_SIGNATURE
    assert ms_a >= 0 and ms_b >= 0


def test_golden_struct_hash(fixed_salt, http):
    """The EIP-712 digest that is actually signed — recomputed through the library's own
    builder, so a domain or field-order change is caught, not just a signature change."""
    from py_order_utils.builders.order_builder import OrderBuilder as UtilsOrderBuilder
    from py_order_utils.signer import Signer as UtilsSigner

    oc = make_client()
    signed, _ = oc.build_signed(spec(), "FOK")
    # negRisk board (the stub returns neg_risk=True) -> negRisk exchange contract.
    builder = UtilsOrderBuilder("0xC5d563A36AE78145C45a50134d48A1215220f80a", 137,
                                UtilsSigner(key=KEY))
    assert builder._create_struct_hash(signed.order) == GOLDEN_STRUCT_HASH


def test_decode_signed_round_trips_price_and_size(fixed_salt, http):
    oc = make_client()
    signed, _ = oc.build_signed(spec(price=0.02, size=4.0), "FOK")
    token_id, side, price, size = decode_signed(signed)
    assert token_id == TOKEN
    assert side == "BUY"
    assert price == pytest.approx(0.02)
    assert size == pytest.approx(4.0)


# --------------------------------------------------------------------------- (b) batch shape
def test_batch_payload_matches_single_order_serialization(fixed_salt, http):
    """post_batch's body must be exactly the client's own per-order serialization, in one
    POST /orders — that is the Tier A/B execution shape."""
    oc = make_client()
    a, _ = oc.build_signed(spec(), "FOK")
    b, _ = oc.build_signed(spec(token=TOKEN_B), "FOK")

    results = oc.post_batch([a, b], "FOK")
    assert len(results) == 2 and all(r.ok for r in results)

    batch_calls = [(u, d) for u, d in http.posts if u.endswith("/orders")]
    assert len(batch_calls) == 1, "the whole set must go in ONE POST /orders"
    url, body = batch_calls[0]
    sent = json.loads(body)

    expected = [order_to_json(x.order, "api-key-uuid", OrderType.FOK, False)
                for x in (PostOrdersArgs(a, OrderType.FOK), PostOrdersArgs(b, OrderType.FOK))]
    assert sent == expected
    # exact bytes, compact separators, as the L2 HMAC is computed over the literal body
    assert body == json.dumps(expected, separators=(",", ":"), ensure_ascii=False)
    # ...and each element equals what post_single would have sent for that order alone.
    assert sent[0] == order_to_json(a, "api-key-uuid", OrderType.FOK, False)


def test_post_single_body_shape(fixed_salt, http):
    oc = make_client()
    signed, _ = oc.build_signed(spec(), "FOK")
    oc.post_single(signed, "FOK")
    url, body = [(u, d) for u, d in http.posts if u.endswith("/order")][0]
    assert json.loads(body) == order_to_json(signed, "api-key-uuid", OrderType.FOK, False)


# --------------------------------------------------------------------------- (c) rails
def test_sell_side_raises(fixed_salt, http):
    oc = make_client()
    with pytest.raises(GuardRailViolation, match="BUY-side only"):
        oc.build_signed(spec(side="SELL"), "FOK")


def test_sell_side_raises_at_post_even_if_signed_elsewhere(fixed_salt, http):
    """Guard bypass attempt: sign a SELL through a deliberately loose client, then hand it
    to a strict one. The rails read the signed order, so it is still refused."""
    loose = make_client(DryRunGuard(buy_only=False, require_book=False))
    signed, _ = loose.build_signed(spec(side="SELL"), "FOK")
    strict = make_client()
    with pytest.raises(GuardRailViolation, match="BUY-side only"):
        strict.post_single(signed, "FOK")
    assert not any(u.endswith("/order") for u, _ in http.posts)


def test_price_above_cap_raises(fixed_salt, http):
    oc = make_client()
    with pytest.raises(GuardRailViolation, match="exceeds dry-run cap"):
        oc.build_signed(spec(price=0.03), "FOK")


def test_price_above_cap_raises_at_post(fixed_salt, http):
    loose = make_client(DryRunGuard(max_price=1.0, require_book=False))
    signed, _ = loose.build_signed(spec(price=0.03), "FOK")
    with pytest.raises(GuardRailViolation, match="exceeds dry-run cap"):
        make_client().post_single(signed, "FOK")


def test_price_at_or_above_half_best_bid_raises(fixed_salt, http):
    """Book says best bid 0.03 -> half is 0.015, and a $0.02 order is above it."""
    http.book = {"bids": [{"price": "0.03", "size": "100"}],
                 "asks": [{"price": "0.05", "size": "100"}]}
    oc = make_client()
    signed, _ = oc.build_signed(spec(price=0.02), "FOK")
    with pytest.raises(GuardRailViolation, match="best bid"):
        oc.post_single(signed, "FOK")
    assert not any(u.endswith("/order") for u, _ in http.posts)


def test_price_exactly_half_best_bid_raises(fixed_salt, http):
    """Boundary: the rail is strict (<), so price == half the bid must fail."""
    http.book = {"bids": [{"price": "0.04", "size": "100"}],
                 "asks": [{"price": "0.06", "size": "100"}]}
    oc = make_client()
    signed, _ = oc.build_signed(spec(price=0.02), "FOK")
    with pytest.raises(GuardRailViolation, match="best bid"):
        oc.post_single(signed, "FOK")


def test_empty_book_raises(fixed_salt, http):
    http.book = {"bids": [], "asks": []}
    oc = make_client()
    signed, _ = oc.build_signed(spec(), "FOK")
    with pytest.raises(GuardRailViolation, match="no bids"):
        oc.post_single(signed, "FOK")


def test_size_above_cap_raises(fixed_salt, http):
    oc = make_client()
    with pytest.raises(GuardRailViolation, match="exceeds dry-run cap"):
        oc.build_signed(spec(size=6.0), "FOK")


def test_size_above_cap_raises_at_post(fixed_salt, http):
    loose = make_client(DryRunGuard(max_size=100.0, require_book=False))
    signed, _ = loose.build_signed(spec(size=6.0), "FOK")
    with pytest.raises(GuardRailViolation, match="exceeds dry-run cap"):
        make_client().post_single(signed, "FOK")


def test_cumulative_notional_cap_raises(fixed_salt, http):
    """$0.02 x 5 shares = $0.10 per order; the 11th crosses the $1.00 cap."""
    oc = make_client()
    signed, _ = oc.build_signed(spec(price=0.02, size=5.0), "FOK")
    for _ in range(10):
        assert oc.post_single(signed, "FOK").ok
    assert oc.notional_spent == pytest.approx(1.00)
    with pytest.raises(GuardRailViolation, match="per-invocation cap"):
        oc.post_single(signed, "FOK")


def test_batch_notional_cap_counts_the_whole_set(fixed_salt, http):
    """The cap is checked over the whole batch BEFORE anything is sent, so an oversized
    set never leaves a partial submission on the book."""
    oc = make_client()
    signed, _ = oc.build_signed(spec(price=0.02, size=5.0), "FOK")
    with pytest.raises(GuardRailViolation, match="per-invocation cap"):
        oc.post_batch([signed] * 11, "FOK")
    assert not any(u.endswith("/orders") for u, _ in http.posts)
    assert oc.notional_spent == 0.0


def test_rails_reread_the_book_at_post_time(fixed_salt, http):
    """A stale cached book must not be what the rail checks: the first post caches a
    healthy book, then the market collapses and the second post has to see it."""
    oc = make_client()
    signed, _ = oc.build_signed(spec(price=0.01), "FOK")
    assert oc.post_single(signed, "FOK").ok
    http.book = {"bids": [{"price": "0.015", "size": "100"}],
                 "asks": [{"price": "0.02", "size": "100"}]}
    with pytest.raises(GuardRailViolation, match="best bid"):
        oc.post_single(signed, "FOK")


def test_failed_post_still_consumes_the_notional_budget(fixed_salt, http, monkeypatch):
    """A post that errors may still have been accepted server-side, so the cap must assume
    it was — otherwise a flaky connection silently multiplies the exposure."""
    oc = make_client()
    signed, _ = oc.build_signed(spec(price=0.02, size=5.0), "FOK")

    def boom(url, headers=None, data=None):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(clob_client_mod, "post", boom)
    r = oc.post_single(signed, "FOK")
    assert r.ok is False and r.status == "error"
    assert oc.notional_spent == pytest.approx(0.10)


def test_bad_order_type_raises(fixed_salt, http):
    oc = make_client()
    with pytest.raises(ValueError, match="order_type"):
        oc.build_signed(spec(), "IOC")


def test_strict_default_is_the_documented_shape():
    assert STRICT_DEFAULT == DryRunGuard(max_price=0.02, max_bid_fraction=0.5, max_size=5.0,
                                         max_notional=1.00, buy_only=True, require_book=True)


def test_from_env_guard_none_falls_back_to_strict(monkeypatch):
    """A caller passing guard=None must not end up unguarded."""
    monkeypatch.setattr(OrderClient, "__init__",
                        lambda self, client, guard=STRICT_DEFAULT: setattr(self, "guard", guard))
    from backend.exec import clob_auth

    monkeypatch.setattr(clob_auth, "load_clob_env",
                        lambda env_file: clob_auth.ClobEnv(KEY, ADDRESS, 0))
    monkeypatch.setattr(clob_auth, "build_client", lambda env: object())
    monkeypatch.setattr(clob_auth, "ensure_api_creds", lambda c, s, p: None)
    assert OrderClient.from_env(guard=None).guard == STRICT_DEFAULT


# --------------------------------------------------------------------------- teardown
def test_context_manager_cancels_on_exit(fixed_salt, http):
    oc = make_client()
    with oc:
        pass
    assert any(u.endswith("/cancel-all") for u in http.deletes)


def test_context_manager_cancels_even_when_body_raises(fixed_salt, http):
    oc = make_client()
    with pytest.raises(RuntimeError):
        with oc:
            raise RuntimeError("boom")
    assert any(u.endswith("/cancel-all") for u in http.deletes)


def test_cancel_all_returns_count(fixed_salt, http):
    assert make_client().cancel_all() == 2


# --------------------------------------------------------------------------- (d) env
def _clear_env(monkeypatch):
    for var in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS",
                "POLYMARKET_SIGNATURE_TYPE"):
        monkeypatch.delenv(var, raising=False)


def test_from_env_empty_lists_all_three_vars(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(MissingCredentials) as exc:
        load_clob_env(env_file=None)
    assert exc.value.missing == ["POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS",
                                 "POLYMARKET_SIGNATURE_TYPE"]
    msg = str(exc.value)
    for var in exc.value.missing:
        assert var in msg


def test_order_client_from_env_propagates_missing_vars(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(MissingCredentials) as exc:
        OrderClient.from_env(env_file=None)
    assert len(exc.value.missing) == 3


def test_partial_env_lists_only_what_is_missing(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", KEY)
    with pytest.raises(MissingCredentials) as exc:
        load_clob_env(env_file=None)
    assert exc.value.missing == ["POLYMARKET_FUNDER_ADDRESS", "POLYMARKET_SIGNATURE_TYPE"]


def test_valid_env_parses(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", KEY)
    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", ADDRESS)
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "2")
    env = load_clob_env(env_file=None)
    assert env.private_key == KEY and env.funder_address == ADDRESS
    assert env.signature_type == 2 and env.chain_id == 137
    assert "0x0123" not in repr(env) and "redacted" in repr(env)


@pytest.mark.parametrize("sig", ["3", "-1", "eoa"])
def test_bad_signature_type_rejected(monkeypatch, sig):
    _clear_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", KEY)
    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", ADDRESS)
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", sig)
    with pytest.raises(ValueError, match="POLYMARKET_SIGNATURE_TYPE"):
        load_clob_env(env_file=None)


@pytest.mark.parametrize("key", ["0xdeadbeef", "not-hex" * 10])
def test_bad_private_key_rejected(monkeypatch, key):
    _clear_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", key)
    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", ADDRESS)
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "0")
    with pytest.raises(ValueError, match="POLYMARKET_PRIVATE_KEY"):
        load_clob_env(env_file=None)


# --------------------------------------------------------------------------- CLI
def test_cli_refuses_without_flag(capsys):
    from backend.exec.dry_run import main

    assert main([]) == 2
    assert "refusing to run" in capsys.readouterr().out


def test_cli_with_flag_but_no_key_exits_1(monkeypatch, capsys):
    from backend.exec.dry_run import main

    _clear_env(monkeypatch)
    # Point at a file that cannot exist, so the test result never depends on the real .env
    # (and can never reach the network even if a key is added there later).
    assert main(["--live-dry-run", "--env-file", "/nonexistent/.env"]) == 1
    out = capsys.readouterr().out
    for var in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS",
                "POLYMARKET_SIGNATURE_TYPE"):
        assert var in out


def test_live_path_end_to_end_against_stubs(fixed_salt, http, monkeypatch, capsys):
    """The authenticated path with the network stubbed out — the only way to exercise
    _live_dry_run on a box with no key. Proves the sequence, not the credentials:
    one POST /order, one POST /orders for the whole batch, one DELETE /cancel-all.
    """
    from backend.exec import clob_auth
    from backend.exec.dry_run import main

    board = {"slug": "test-board", "legs": [
        {"t": "a", "yes": TOKEN, "no": "1"},
        {"t": "b", "yes": TOKEN_B, "no": "2"},
        {"t": "c", "yes": "9" + TOKEN[1:], "no": "3"},
    ]}
    monkeypatch.setattr("backend.exec.dry_run.load_boards", lambda *a, **k: [board])
    monkeypatch.setattr(clob_auth, "load_clob_env",
                        lambda env_file=None: clob_auth.ClobEnv(KEY, ADDRESS, 0))
    monkeypatch.setattr(clob_auth, "build_client", lambda env: ClobClient(
        "https://clob.polymarket.com", chain_id=137, key=KEY,
        creds=ApiCreds("api-key-uuid", "c2VjcmV0", "pass"), signature_type=0, funder=ADDRESS))
    monkeypatch.setattr(clob_auth, "ensure_api_creds", lambda c, s, p: None)

    assert main(["--live-dry-run", "--slug", "test-board", "--order-type", "GTC"]) == 0

    assert len([u for u, _ in http.posts if u.endswith("/order")]) == 1
    assert len([u for u, _ in http.posts if u.endswith("/orders")]) == 1
    assert len([u for u in http.deletes if u.endswith("/cancel-all")]) >= 1
    out = capsys.readouterr().out
    assert "post_batch[1]" in out and "cancel_all" in out


# --------------------------------------------------------------------------- creds cache
def test_api_creds_cached_once(tmp_path, monkeypatch):
    from backend.exec import clob_auth

    calls = []

    class FakeClient:
        creds = None

        def get_address(self):
            return ADDRESS

        def create_or_derive_api_creds(self):
            calls.append(1)
            return ApiCreds("k", "s", "p")

        def set_api_creds(self, creds):
            self.creds = creds

    path = str(tmp_path / ".clob_creds.json")
    c1, c2 = FakeClient(), FakeClient()
    clob_auth.ensure_api_creds(c1, 0, path)
    clob_auth.ensure_api_creds(c2, 0, path)
    assert len(calls) == 1                    # second call served from disk
    assert c2.creds == ApiCreds("k", "s", "p")
    # a different signature type must NOT reuse the cached creds
    clob_auth.ensure_api_creds(FakeClient(), 1, path)
    assert len(calls) == 2
    assert oct(__import__("os").stat(path).st_mode & 0o777) == "0o600"
