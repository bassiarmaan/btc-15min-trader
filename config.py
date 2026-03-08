from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Polymarket
    pm_api_key: str = ""
    pm_secret: str = ""
    pm_passphrase: str = ""
    pm_clob_url: str = "https://clob.polymarket.com"
    pm_gamma_url: str = "https://gamma-api.polymarket.com"

    # Paper trading
    initial_balance: float = 10_000.0
    max_position_pct: float = 0.10
    max_concurrent: int = 5
    min_position_usd: float = 0.50  # min order size (use 0.50 for small bankrolls)
    daily_loss_limit: float = -999_999.0  # effectively off; set e.g. -200 to cap daily loss
    max_drawdown_pct: float = 1.0  # decimal, e.g. 0.18 = 18% — stop new trades when (peak - balance) / peak >= this

    # Strategy thresholds
    min_spread_pct: float = 2.0
    min_edge_pct: float = 2.0
    min_confidence: float = 0.60
    # Fair value / edge quality (reduce false edges and overconfidence)
    btc_pricing_vol: float = 0.70  # annual vol for binary option fair value (higher = more uncertainty, fair closer to 0.5)
    min_edge_prob: float = 0.03  # require (fair - ask) >= this in probability (avoids tiny edges that are noise)
    fair_value_shrink: float = 0.10  # shrink fair toward 0.5 (0=off, 0.10=10% more conservative)
    min_entry_price: float = 0.05  # skip YES/NO below this (avoids 1¢ lottery bets that often go -100%)
    hard_min_entry_price: float = 0.10  # hard safety floor for entry price even if min_entry_price is set lower
    min_net_edge_pct: float = 2.0  # require edge after estimated execution cost to exceed this
    max_btc_stale_s: float = 3.0  # stop signal generation if CEX price feed is stale
    max_market_stale_s: float = 4.0  # stop signal generation if market feed is stale
    min_seconds_after_market_open: float = 30.0  # don't take first position until this long after first signal for that market (avoids hasty first tick)
    # Direction + value: only take YES when spot >= strike, only NO when spot <= strike (reduces fighting the trend)
    require_directional_alignment: bool = True
    momentum_lookback_seconds: float = 0.0  # if > 0, YES only when recent price trend up, NO only when down (0 = disable)
    min_fair_probability: float = 0.55  # model must be at least this confident in the direction (prevents 50/50 flip-flop)
    kelly_fraction: float = 0.25
    max_signal_kelly_fraction: float = 0.08  # hard cap at signal layer before risk sizing
    kelly_confidence_weight: float = 1.0  # scale Kelly size by confidence to reduce model overconfidence risk
    slippage_bps: float = 50.0
    low_price_entry_threshold: float = 0.20  # extra sizing guard for cheap contracts
    low_price_max_position_pct: float = 0.03  # max fraction of balance for cheap contracts
    early_exit_profit_pct: float = 0.80  # close when unrealized profit >= this % of cost (0 = disabled)
    early_exit_loss_pct: float = 0.50  # close when unrealized loss >= this % of cost (0 = disabled)
    reversal_on_loss_cap: bool = True  # after cut-loss, place one bet on opposite side in same cycle
    reversal_bet_fraction: float = 0.50  # size reversal bet as this fraction of closed position cost (0.5 = less aggressive)

    # Simulation mode (generates synthetic PM markets from real BTC prices)
    simulation_mode: bool = True
    sim_lag_ms: int = 500
    sim_market_interval_s: int = 300
    sim_num_strikes: int = 5
    sim_strike_spacing: float = 250.0
    sim_btc_annual_vol: float = 0.70

    # Kalshi
    kalshi_mode: bool = True
    kalshi_api_key_id: str = ""
    kalshi_private_key: str = ""
    kalshi_api_base: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_series: str = "KXBTC15M"
    kalshi_poll_s: float = 2.0
    kalshi_live_execution: bool = False  # place real orders on Kalshi (requires API keys)
    kalshi_live_max_order_usd: float = 5.0  # hard per-order cap to mirror older $5 behaviour
    kalshi_live_max_contracts: int = 1  # hard cap on contracts per live buy order

    # Storage
    db_path: str = "data/trades.db"

    # Web dashboard
    web_port: int = 8080

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
