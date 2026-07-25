"""链上交易签名与发送：统一走 wallet 模块的私钥实例。"""

from __future__ import annotations

import logging
from typing import Any

from wallet import WalletConfigError, get_keypair, require_wallet_for_live, sign_message, wallet_status

logger = logging.getLogger("pumpfun.chain")


class ChainSigner:
    """Pump.fun / Solana 交易签名器。

    所有实盘签名必须经由本类（或 wallet.require_wallet_for_live），
    禁止在业务代码中直接读取环境变量拼装密钥。
    """

    def __init__(self) -> None:
        self._kp: Any | None = None

    @property
    def ready(self) -> bool:
        try:
            self.ensure_loaded()
            return True
        except WalletConfigError:
            return False

    def ensure_loaded(self) -> Any:
        if self._kp is None:
            self._kp = require_wallet_for_live()
        return self._kp

    @property
    def pubkey(self) -> str:
        return str(self.ensure_loaded().pubkey())

    def sign(self, message: bytes) -> bytes:
        return sign_message(message)

    def sign_transaction(self, tx: Any) -> Any:
        """对 VersionedTransaction / Transaction 签名。

        兼容 solders 常见接口：tx.sign([keypair])。
        """
        kp = self.ensure_loaded()
        if hasattr(tx, "sign"):
            try:
                tx.sign([kp])
                return tx
            except TypeError:
                tx.sign(kp)
                return tx
        raise WalletConfigError(f"不支持的交易对象类型: {type(tx)}")

    def status(self) -> dict[str, Any]:
        st = wallet_status()
        st["signer_ready"] = self.ready
        return st


# 进程内单例：全模块共用同一私钥实例
signer = ChainSigner()


def get_signer() -> ChainSigner:
    return signer


def keypair_for_live() -> Any:
    """执行层获取 Keypair 的唯一推荐入口。"""
    return get_signer().ensure_loaded()
