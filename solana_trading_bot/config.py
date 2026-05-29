"""Chargement et validation de la configuration (config.yaml + .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # python-dotenv optionnel
    pass


@dataclass
class Secrets:
    """Secrets lus depuis l'environnement (jamais depuis le YAML)."""

    birdeye_api_key: str = ""
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    wallet_private_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            birdeye_api_key=os.getenv("BIRDEYE_API_KEY", "").strip(),
            solana_rpc_url=os.getenv(
                "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
            ).strip(),
            wallet_private_key=os.getenv("WALLET_PRIVATE_KEY", "").strip(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        )


class Config:
    """Wrapper d'accès à la configuration avec accès par chemin pointé."""

    def __init__(self, data: dict[str, Any], secrets: Secrets):
        self._data = data
        self.secrets = secrets

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"Fichier de configuration introuvable : {path}. "
                "Copie config.yaml fourni dans le dépôt."
            )
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        # Fusionne les profils de stratégie/risque actifs dans la config.
        from .strategies import apply_profiles

        data = apply_profiles(data)
        cfg = cls(data, Secrets.from_env())
        cfg.validate()
        return cfg

    @property
    def active_strategy(self) -> str:
        return (self.get("_active_profile.strategy")
                or self.get("strategy.active") or "default")

    @property
    def active_risk_profile(self) -> str:
        return (self.get("_active_profile.risk_profile")
                or self.get("strategy.risk_profile") or "default")

    def get(self, dotted: str, default: Any = None) -> Any:
        """Accès du type cfg.get('risk.stop_loss_pct')."""
        node: Any = self._data
        for key in dotted.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    @property
    def mode(self) -> str:
        return str(self.get("mode", "paper")).lower()

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    def validate(self) -> None:
        """Garde-fous basiques pour éviter les configs dangereuses."""
        mc_min = self.get("universe.market_cap_min", 0)
        mc_max = self.get("universe.market_cap_max", 0)
        if mc_min >= mc_max:
            raise ValueError("universe.market_cap_min doit être < market_cap_max")

        if self.mode not in ("paper", "live"):
            raise ValueError("mode doit être 'paper' ou 'live'")

        if self.is_live and not self.secrets.wallet_private_key:
            raise ValueError(
                "Mode LIVE activé mais WALLET_PRIVATE_KEY est vide dans .env. "
                "Abandon par sécurité."
            )

        ps = self.get("risk.position_size_pct", 0)
        if not 0 < ps <= 100:
            raise ValueError("risk.position_size_pct doit être dans (0, 100]")
