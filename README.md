# Ultra V3 — Bot de trading & analyse small-cap Solana

Bot **modulaire** de trading et d'analyse automatique sur **Solana**, ciblant
les paires dont la **market cap est comprise entre 500 000 $ et 3 000 000 $**.
Découverte de tokens, analyse technique multi-indicateurs, filtres de
liquidité, contrôles anti-rug / honeypot, gestion du risque et **paper trading**
réaliste (slippage + frais simulés).

> ⚠️ **Avertissement.** Le trading de crypto-actifs — en particulier les
> small caps / memecoins Solana — est extrêmement risqué. Ce logiciel est
> fourni à des fins éducatives et de recherche. Il démarre en **paper trading**
> (aucun fonds réel). N'activez le mode réel qu'en pleine connaissance de cause
> et avec des montants que vous pouvez perdre. Aucune garantie de profit.

---

## 🧱 Architecture

```
solana_trading_bot/
├── config.py            # chargement config.yaml + .env (secrets)
├── logger.py            # logs console (rich) + fichier
├── models.py            # dataclasses : TokenPair, Position, Trade, ...
├── engine.py            # orchestrateur (pipeline complet par cycle)
├── reporting.py         # statistiques de performance
├── __main__.py          # CLI (run / once / scan / stats)
├── clients/
│   ├── http.py          # HTTP avec retry + back-off (429)
│   ├── dexscreener.py   # découverte de paires + données de marché
│   ├── birdeye.py       # OHLCV + sécurité on-chain + holders
│   └── jupiter.py       # quotes/route (price impact, anti-honeypot)
├── strategies.py        # profils stratégie (scalping/swing) + risque
├── analysis/
│   ├── indicators.py    # RSI, EMA, MACD, Bollinger, ATR (pandas/numpy)
│   ├── signals.py       # score composite 0-100 + signal BUY/HOLD/AVOID
│   └── recommendation.py# note A+→F + plan d'action (entrée/stop/TP/taille)
├── safety/
│   └── filters.py       # filtre univers (MC+liquidité) + anti-rug/honeypot
├── trading/
│   ├── risk.py          # sizing, exposition, coupe-circuit, SL/TP/trailing
│   └── portfolio.py     # cash, positions, exécution paper (PnL réaliste)
└── storage/
    └── database.py      # persistance SQLite (trades, positions, équité)
```

### Pipeline d'un cycle

1. **Découverte** — DexScreener agrège les paires Solana (tokens émergents via
   `token-profiles` / `token-boosts` + recherches génériques).
2. **Filtre 1 (univers)** — market cap 500k–3M, liquidité, volume 24h, ratio
   volume/liquidité, nombre de transactions, âge de la paire.
3. **Analyse technique** — OHLCV Birdeye → RSI, EMA (9/21/50), MACD, Bollinger,
   ATR, volume → **score composite pondéré** sur 5 dimensions.
4. **Filtre 2 (sécurité)** — anti-honeypot (route de vente Jupiter), impact
   prix, mint/freeze authority révoquées, concentration des holders, nb holders.
5. **Décision** — sizing selon le risque, vérifs d'exposition, **achat (paper)**.
6. **Gestion des positions** — stop-loss, take-profit partiel, **trailing stop**,
   sortie temporelle, coupe-circuit de perte journalière.

---

## 🚀 Installation

```bash
# 1. Dépendances
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Secrets (optionnels mais recommandés)
cp .env.example .env
#   - BIRDEYE_API_KEY : débloque l'analyse OHLCV complète + sécurité on-chain
#   - (le mode paper fonctionne sans aucune clé)
```

## ▶️ Utilisation

```bash
# Boucle continue (paper trading)
python -m solana_trading_bot run

# Un seul cycle (utile en cron)
python -m solana_trading_bot once

# Découverte + analyse SANS trader (exploration)
python -m solana_trading_bot scan

# Tableau de NOTATION des tokens (note A+→F + plan par token)
python -m solana_trading_bot rank

# Rapport de performance
python -m solana_trading_bot stats
```

> Sans `BIRDEYE_API_KEY`, le bot fonctionne en **analyse dégradée** (momentum
> DexScreener uniquement). Ajoutez une clé Birdeye pour activer les indicateurs
> techniques complets et les contrôles de sécurité on-chain.

---

## ⚙️ Configuration (`config.yaml`)

Tout est paramétrable sans toucher au code. Sections principales :

| Section       | Rôle |
|---------------|------|
| `universe`    | Bornes de market cap (500k–3M), quotes acceptées (SOL/USDC) |
| `liquidity`   | Liquidité min, volume min, ratio vol/liq, âge, nb txns |
| `safety`      | Mint/freeze révoquées, concentration holders, impact prix max |
| `analysis`    | Périodes des indicateurs, poids du score, seuil d'entrée |
| `risk`        | Taille de position, max positions, SL/TP/trailing, coupe-circuit |
| `storage`     | Chemins SQLite / log |

### Score composite (0–100)

| Dimension          | Poids | Mesure |
|--------------------|-------|--------|
| `trend`            | 0.25  | Alignement EMA 9/21/50, prix vs tendance |
| `momentum`         | 0.25  | RSI (zone 50–70 idéale), MACD histogram |
| `volume`           | 0.20  | Pic de volume vs moyenne mobile |
| `volatility`       | 0.10  | Position dans les bandes de Bollinger, ATR |
| `liquidity_health` | 0.20  | Ratio volume/liquidité, profondeur du pool |

Entrée déclenchée quand `score ≥ entry_score_threshold` **et** réussite du
filtre anti-rug.

---

## 🎯 Stratégies, risque & notation

### Profils de stratégie (`strategy.active`)

Le style de trading est sélectionnable dans `config.yaml`. Chaque profil
écrase les sections `analysis` / `risk` / `loop` :

| Profil      | Timeframe | Stop | Take-profit (paliers)      | Détention |
|-------------|-----------|------|----------------------------|-----------|
| `scalping`  | 5 min     | serré (ATR ×1.3, ~5%) | +6% / +12% / +25% | ~4 h max |
| `swing`     | 1 h       | large (ATR ×2.0, ~15%)| +30% / +60% / +120% | ~4 j max |

### Profils de risque (`strategy.risk_profile`)

Appliqués **par-dessus** la stratégie, ils ajustent l'appétit au risque :

| Profil         | % risqué/trade | Max positions | Exposition max | Seuil entrée |
|----------------|----------------|---------------|----------------|--------------|
| `conservative` | 1.0%           | 4             | 40%            | 75           |
| `moderate`     | 1.5%           | 6             | 60%            | (défaut)     |
| `aggressive`   | 3.0%           | 10            | 85%            | 62           |

### Note par token (A+ → F)

Chaque token analysé reçoit une **note** synthétique dérivée du score :

| Note | A+   | A    | B    | C    | D    | F    |
|------|------|------|------|------|------|------|
| Score| ≥88  | ≥80  | ≥72  | ≥63  | ≥52  | <52  |

### Plan d'action de scalping

Pour tout setup actionnable (`STRONG_BUY` / `BUY`), le bot produit un plan
concret, affiché par `rank` et journalisé avant chaque entrée :

- **Action** : `STRONG_BUY` · `BUY` · `WATCH` · `AVOID`
- **Entrée** : prix de marché courant
- **Stop-loss** : adapté à la volatilité (ATR) ou % du profil
- **Paliers de take-profit** : niveaux de prix + fraction vendue à chaque palier
- **Taille de position** : calculée sur le **risque** (`risk_per_trade_pct`),
  bornée par la taille max, le cash et l'exposition restante
- **Risque/récompense (R/R)** et **durée de détention estimée**
- **Confiance** : cohérence entre les composantes du score

Ces niveaux sont **attachés à la position** : la gestion des sorties exécute
le stop et les paliers de TP au prix planifié, puis arme un trailing stop sur
le reliquat.

---

## 🛡️ Sécurité & gestion du risque

- **Anti-honeypot** : vérifie qu'une route de **vente** existe via Jupiter avant
  tout achat ; rejette si la revente est impossible.
- **Impact prix** : rejette si l'achat de la taille de position dépasse le seuil.
- **On-chain** (Birdeye) : mint/freeze authority révoquées, concentration des
  10 plus gros holders, nombre minimum de détenteurs.
- **Risque** : taille de position en % du capital, plafond absolu par trade,
  exposition cumulée max, **coupe-circuit de perte journalière**.
- **Sorties auto** : stop-loss dur, take-profit partiel, trailing stop armé
  après activation, sortie temporelle (`max_hold_hours`).

---

## 🔴 Mode LIVE (réel)

Le mode `live` est **présent mais volontairement désactivé** par un garde-fou :
`JupiterClient.execute_swap` lève `NotImplementedError`. Pour l'activer il faut,
en pleine connaissance des risques :

1. Installer `solders` / `solana-py`.
2. Implémenter la construction + **signature** de la transaction de swap
   (`POST /swap/v1/swap` de Jupiter) avec votre `WALLET_PRIVATE_KEY`.
3. Gérer l'envoi RPC, le priority fee, et la confirmation on-chain.
4. Retirer le garde-fou dans `clients/jupiter.py` et `trading/portfolio.py`.

**Validez d'abord longuement votre stratégie en paper trading.**

---

## 🧪 Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

Les tests couvrent les indicateurs, le moteur de signal (mode complet et
dégradé), le sizing/risque et un aller-retour complet de paper trading.

---

## 📊 Données persistées (SQLite)

- `trades` — historique complet (entrées/sorties, frais, PnL, raison)
- `positions` — positions ouvertes (reprise d'état au redémarrage)
- `equity_curve` — courbe d'équité dans le temps
- `meta` — cash, PnL réalisé, équité de référence journalière

---

*Construit en Python. Sources de données : DexScreener, Birdeye, Jupiter.*
