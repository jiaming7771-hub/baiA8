"""钱包私钥：仅从环境变量加载，禁止硬编码；LIVE 切换强校验。"""

from __future__ import annotations

import base64

import pytest

import wallet


@pytest.fixture(autouse=True)
def _clean_wallet(monkeypatch, tmp_path):
    wallet.clear_wallet_cache()
    monkeypatch.setattr(wallet, "_loaded_dotenv", True)  # 跳过读真实 .env
    monkeypatch.setattr(wallet, "_ENV_CANDIDATES", (tmp_path / "nope.env",))
    for name in wallet.ENV_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield
    wallet.clear_wallet_cache()


def _make_keypair_bytes() -> tuple[bytes, str]:
    from solders.keypair import Keypair

    kp = Keypair()
    secret = bytes(kp)
    # base58
    import base58

    return secret, base58.b58encode(secret).decode()


def test_missing_key_raises():
    assert wallet.has_wallet_secret() is False
    with pytest.raises(wallet.WalletConfigError):
        wallet.get_keypair()


def test_load_base58_keypair(monkeypatch):
    secret, b58 = _make_keypair_bytes()
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", b58)
    kp = wallet.get_keypair()
    assert bytes(kp) == secret
    st = wallet.wallet_status()
    assert st["configured"] is True
    assert st["load_ok"] is True
    assert st["env_var"] == "SOLANA_PRIVATE_KEY"
    assert st["pubkey"]
    # 状态接口绝不能带回私钥
    blob = str(st)
    assert b58 not in blob


def test_load_json_array_keypair(monkeypatch):
    secret, _ = _make_keypair_bytes()
    monkeypatch.setenv("WALLET_PRIVATE_KEY", str(list(secret)))
    kp = wallet.get_keypair()
    assert bytes(kp) == secret
    assert wallet.wallet_status()["env_var"] == "WALLET_PRIVATE_KEY"


def test_wallet_key_takes_priority(monkeypatch):
    s1, b1 = _make_keypair_bytes()
    s2, b2 = _make_keypair_bytes()
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", b1)
    monkeypatch.setenv("WALLET_PRIVATE_KEY", b2)
    kp = wallet.get_keypair()
    assert bytes(kp) == s2  # WALLET_PRIVATE_KEY 优先


def test_sign_message(monkeypatch):
    _, b58 = _make_keypair_bytes()
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", b58)
    sig = wallet.sign_message(b"hello-pump")
    assert isinstance(sig, (bytes, bytearray))
    assert len(sig) == 64


def test_no_hardcoded_secrets_in_wallet_module():
    src = (wallet.__file__ and open(wallet.__file__, encoding="utf-8").read()) or ""
    # 粗检：源码里不应出现长 base58 形态的疑似密钥赋值
    assert "SOLANA_PRIVATE_KEY=" not in src.replace('"', "").replace("'", "")
    assert "private_key =" not in src.lower() or "os.getenv" in src


def test_live_switch_requires_wallet(monkeypatch, tmp_path):
    from pumpfun import config as C
    from pumpfun.main import PumpScavengerBot

    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "TRADING_LOGS_DIR", tmp_path)
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(C, "STOP_FILE", tmp_path / "STOP.txt")
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(C, "EXEC_LOG_FILE", tmp_path / "exec.log")
    monkeypatch.setattr(C, "LIVE_CONFIRM", True)

    # 本机 .env 可能配置了真实钱包：先清干净再断言「无私钥拒绝 LIVE」
    monkeypatch.delenv("WALLET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("SOLANA_PRIVATE_KEY", raising=False)
    wallet.clear_wallet_cache()
    from pumpfun.chain import signer

    monkeypatch.setattr(signer, "_kp", None)

    bot = PumpScavengerBot()
    bot.broker.dry_run = True
    with pytest.raises(Exception):
        bot.set_dry_run(False)  # 无私钥 → 拒绝 LIVE
    assert bot.broker.dry_run is True

    _, b58 = _make_keypair_bytes()
    monkeypatch.setenv("WALLET_PRIVATE_KEY", b58)
    wallet.clear_wallet_cache()

    # 跳过真实 RPC 余额横幅
    monkeypatch.setattr(
        bot,
        "_log_live_banner",
        lambda: None,
    )
    bot.set_dry_run(False)
    assert bot.broker.dry_run is False
    snap = bot.snapshot()
    assert snap["wallet"]["configured"] is True
    assert snap["wallet"]["load_ok"] is True


def test_live_switch_requires_confirm(monkeypatch, tmp_path):
    from pumpfun import config as C
    from pumpfun.main import PumpScavengerBot

    monkeypatch.setattr(C, "LIVE_CONFIRM", False)
    bot = PumpScavengerBot()
    bot.broker.dry_run = True
    with pytest.raises(RuntimeError, match="PUMP_LIVE_CONFIRM"):
        bot.set_dry_run(False)
    assert bot.broker.dry_run is True
