"""实盘钱包密钥：仅从环境变量 / .env 读取，禁止硬编码。

支持变量名（按优先级）：
  WALLET_PRIVATE_KEY  >  SOLANA_PRIVATE_KEY  >  PUMP_WALLET_PRIVATE_KEY

支持格式：
  - Base58 编码的 64 字节 secret key（Phantom / Solana CLI 常见导出）
  - JSON 字节数组，如 [12,34,...,99]（solana-keygen 文件内容）
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("wallet")

# 候选环境变量名（前者优先；用户约定以 WALLET_PRIVATE_KEY 为主）
ENV_KEY_NAMES = (
    "WALLET_PRIVATE_KEY",
    "SOLANA_PRIVATE_KEY",
    "PUMP_WALLET_PRIVATE_KEY",
)

_ROOT = Path(__file__).resolve().parents[1]  # 项目根 a8/
_BACKEND = Path(__file__).resolve().parent
_ENV_CANDIDATES = (
    _ROOT / ".env",
    _BACKEND / ".env",
    _ROOT / "simlab" / ".env",
)

_lock = threading.Lock()
_loaded_dotenv = False
_keypair_cache: Any | None = None
_pubkey_cache: str | None = None
_secret_present = False
_secret_source: str | None = None


class WalletConfigError(RuntimeError):
    """钱包配置错误（缺密钥 / 格式非法）。"""


def load_dotenv_files(*, override: bool = False) -> list[str]:
    """加载项目内 .env（不覆盖已在 shell 中显式 export 的变量，除非 override=True）。"""
    global _loaded_dotenv
    loaded: list[str] = []
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning("未安装 python-dotenv，仅读取进程环境变量；建议: pip install python-dotenv")
        _loaded_dotenv = True
        return loaded

    with _lock:
        for path in _ENV_CANDIDATES:
            if path.is_file():
                load_dotenv(path, override=override)
                loaded.append(str(path))
        _loaded_dotenv = True
    if loaded:
        logger.info("已加载 .env: %s", ", ".join(loaded))
    return loaded


def _ensure_dotenv() -> None:
    if not _loaded_dotenv:
        load_dotenv_files()


def _raw_secret_from_env() -> tuple[str | None, str | None]:
    """返回 (secret, env_var_name)；找不到则 (None, None)。"""
    _ensure_dotenv()
    for name in ENV_KEY_NAMES:
        val = os.getenv(name, "").strip()
        if val:
            return val, name
    return None, None


def _parse_secret_bytes(raw: str) -> bytes:
    """将 Base58 或 JSON 数组解析为 64 字节 secret。"""
    s = raw.strip()
    if not s:
        raise WalletConfigError("私钥为空")

    # JSON 字节数组
    if s.startswith("["):
        try:
            arr = json.loads(s)
        except json.JSONDecodeError as exc:
            raise WalletConfigError(f"私钥 JSON 解析失败: {exc}") from exc
        if not isinstance(arr, list) or not arr:
            raise WalletConfigError("私钥 JSON 必须是非空字节数组")
        data = bytes(int(x) & 0xFF for x in arr)
        if len(data) not in (64, 32):
            raise WalletConfigError(f"私钥字节长度应为 64 或 32，实际 {len(data)}")
        return data

    # Base58
    try:
        import base58  # type: ignore
    except ImportError:
        # 轻量 fallback：尝试 solders 自带解析
        try:
            from solders.keypair import Keypair  # type: ignore

            kp = Keypair.from_base58_string(s)
            return bytes(kp)
        except Exception as exc:
            raise WalletConfigError(
                "无法解析 Base58 私钥：请安装 base58 或 solders（pip install base58 solders）"
            ) from exc

    try:
        data = base58.b58decode(s)
    except Exception as exc:
        raise WalletConfigError(f"Base58 私钥解码失败: {exc}") from exc
    if len(data) not in (64, 32):
        raise WalletConfigError(f"Base58 私钥解码后长度应为 64 或 32，实际 {len(data)}")
    return data


def _keypair_from_bytes(secret: bytes) -> Any:
    try:
        from solders.keypair import Keypair  # type: ignore
    except ImportError as exc:
        raise WalletConfigError(
            "实盘签名需要 solders：pip install solders"
        ) from exc

    if len(secret) == 64:
        return Keypair.from_bytes(secret)
    if len(secret) == 32:
        return Keypair.from_seed(secret)
    raise WalletConfigError(f"不支持的密钥长度: {len(secret)}")


def has_wallet_secret() -> bool:
    """是否配置了私钥（不校验格式、不实例化）。"""
    raw, _ = _raw_secret_from_env()
    return bool(raw)


def get_keypair(*, force_reload: bool = False) -> Any:
    """返回 solders.Keypair；失败抛 WalletConfigError。私钥永不写入日志。"""
    global _keypair_cache, _pubkey_cache, _secret_present, _secret_source

    with _lock:
        if _keypair_cache is not None and not force_reload:
            return _keypair_cache

        raw, source = _raw_secret_from_env()
        if not raw:
            raise WalletConfigError(
                "未配置钱包私钥。请在 .env 中设置 SOLANA_PRIVATE_KEY 或 WALLET_PRIVATE_KEY "
                "（参考 .env.example），切勿写入源代码。"
            )
        secret = _parse_secret_bytes(raw)
        kp = _keypair_from_bytes(secret)
        _keypair_cache = kp
        _pubkey_cache = str(kp.pubkey())
        _secret_present = True
        _secret_source = source
        # 安全：立即丢弃明文副本引用（Python 无法保证内存擦除，但避免继续持有 raw）
        del raw, secret
        logger.info(
            "钱包已加载 source=%s pubkey=%s…%s",
            source,
            _pubkey_cache[:4],
            _pubkey_cache[-4:],
        )
        return _keypair_cache


def get_pubkey_str() -> str | None:
    """已加载则返回公钥字符串；未配置返回 None。"""
    if not has_wallet_secret():
        return None
    try:
        kp = get_keypair()
        return str(kp.pubkey())
    except WalletConfigError:
        return None


def require_wallet_for_live() -> Any:
    """实盘交易前强制校验：必须能成功实例化 Keypair。"""
    if not has_wallet_secret():
        raise WalletConfigError(
            "LIVE 模式需要钱包私钥：请在 .env 配置 SOLANA_PRIVATE_KEY / WALLET_PRIVATE_KEY"
        )
    return get_keypair()


def sign_message(message: bytes) -> bytes:
    """用实盘钱包对消息签名（交易模块统一入口）。"""
    kp = require_wallet_for_live()
    sig = kp.sign_message(message)
    return bytes(sig)


def wallet_status() -> dict[str, Any]:
    """供 API/面板展示的安全状态（不含私钥）。"""
    raw, source = _raw_secret_from_env()
    configured = bool(raw)
    pubkey = None
    load_ok = False
    error = None
    if configured:
        try:
            pubkey = get_pubkey_str()
            load_ok = pubkey is not None
        except WalletConfigError as exc:
            error = str(exc)
    return {
        "configured": configured,
        "load_ok": load_ok,
        "env_var": source,
        "pubkey": pubkey,
        "pubkey_short": (f"{pubkey[:4]}…{pubkey[-4:]}" if pubkey else None),
        "error": error,
    }


def clear_wallet_cache() -> None:
    """测试或轮换密钥后清空缓存。"""
    global _keypair_cache, _pubkey_cache, _secret_present, _secret_source
    with _lock:
        _keypair_cache = None
        _pubkey_cache = None
        _secret_present = False
        _secret_source = None
