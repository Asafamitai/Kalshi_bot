import json
from decimal import Decimal

from kalshi_bot.config import Config
from kalshi_bot.fees import fee_per_contract, order_cost, order_fee

D = Decimal


def test_fee_is_quadratic_and_rounded_up_per_order():
    assert fee_per_contract(D("0.50")) == D("0.0175")
    assert order_fee(D("0.86"), 10) == D("0.09")  # 0.0843 rounds up
    assert order_fee(D("0.86"), 0) == D("0.00")
    assert order_cost(D("0.86"), 10) == D("8.69")


def test_reads_ladder_coherence_variable_names():
    accounts = [{"name": "spare", "key_id": "a", "private_key_pem": "x"},
                {"name": "main", "key_id": "b", "private_key_pem": "line1\\nline2"}]
    cfg = Config.from_env({
        "KALSHI_ACCOUNTS_JSON": json.dumps(accounts),
        "LADDER_TELEGRAM_BOT_TOKEN": "tok",
        "LADDER_TELEGRAM_CHAT_ID": "42",
    })
    assert (cfg.kalshi_key_id, cfg.kalshi_private_key) == ("b", "line1\nline2")
    assert cfg.telegram_token == "tok" and cfg.telegram_chat_id == "42"
    assert cfg.telegram_allowed_user_ids == frozenset({"42"})


def test_account_choice_and_plain_names_win():
    accounts = [{"name": "main", "key_id": "b", "private_key_pem": "p"},
                {"name": "sub", "key_id": "c", "private_key_pem": "q"}]
    cfg = Config.from_env({"KALSHI_ACCOUNTS_JSON": json.dumps(accounts), "LADDER_KALSHI_ACCOUNT": "sub",
                           "TELEGRAM_BOT_TOKEN": "plain", "LADDER_TELEGRAM_BOT_TOKEN": "ladder"})
    assert cfg.kalshi_key_id == "c" and cfg.telegram_token == "plain"


def test_secrets_stay_out_of_repr():
    cfg = Config.from_env({"KALSHI_API_KEY_ID": "k", "KALSHI_PRIVATE_KEY": "SECRET", "TELEGRAM_BOT_TOKEN": "TOK"})
    assert "SECRET" not in repr(cfg) and "TOK" not in repr(cfg)
