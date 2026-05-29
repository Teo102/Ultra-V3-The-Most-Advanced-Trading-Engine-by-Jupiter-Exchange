"""Profils de stratégie et de risque.

Un *profil de stratégie* (ex: scalping, swing) et un *profil de risque*
(conservateur / modéré / agressif) sont sélectionnés dans config.yaml. Au
chargement, leurs valeurs sont fusionnées dans la config : elles écrasent
les sections `analysis` et `risk` par défaut, sans toucher au code.

Ainsi le même bot peut passer de "scalping agressif" à "swing prudent"
en changeant deux lignes de configuration.
"""

from __future__ import annotations

from .logger import get_logger

log = get_logger("strategy")


def _deep_merge(base: dict, override: dict) -> dict:
    """Fusion récursive : override gagne, dicts fusionnés en profondeur."""
    out = dict(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def apply_profiles(data: dict) -> dict:
    """Applique le profil de stratégie + de risque actifs sur la config.

    Retourne une nouvelle config (dict) avec `analysis` et `risk` ajustés,
    et un bloc `_active_profile` décrivant ce qui a été appliqué.
    """
    strat_cfg = data.get("strategy") or {}
    active = strat_cfg.get("active")
    risk_profile = strat_cfg.get("risk_profile")

    profiles = strat_cfg.get("profiles") or {}
    risk_profiles = strat_cfg.get("risk_profiles") or {}

    merged = dict(data)
    applied = {"strategy": None, "risk_profile": None}

    # 1) Profil de stratégie -> écrase analysis + risk + loop
    if active and active in profiles:
        prof = profiles[active]
        for section in ("analysis", "risk", "loop"):
            if section in prof:
                merged[section] = _deep_merge(merged.get(section, {}),
                                              prof[section])
        applied["strategy"] = active
        log.info("Profil de stratégie actif : '%s'", active)
    elif active:
        log.warning("Profil de stratégie '%s' introuvable — défauts utilisés",
                    active)

    # 2) Profil de risque -> ajuste risk + seuil d'entrée
    if risk_profile and risk_profile in risk_profiles:
        rp = risk_profiles[risk_profile]
        if "risk" in rp:
            merged["risk"] = _deep_merge(merged.get("risk", {}), rp["risk"])
        if "analysis" in rp:
            merged["analysis"] = _deep_merge(merged.get("analysis", {}),
                                             rp["analysis"])
        applied["risk_profile"] = risk_profile
        log.info("Profil de risque actif : '%s'", risk_profile)
    elif risk_profile:
        log.warning("Profil de risque '%s' introuvable — défauts utilisés",
                    risk_profile)

    merged["_active_profile"] = applied
    return merged
