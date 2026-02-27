from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Polymarket
    pm_api_key: str = ""
    pm_secret: str = ""
    pm_passphrase: str = ""
    pm_clob_url: str = "https://clob.polymarket.com"
    pm_gamma_url: str = "https://gamma-api.polymarket.com"

    # Binance public websocket (no key needed)
    binance_ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@trade"

    # Paper trading
    initial_balance: float = 10_000.0
    max_position_pct: float = 0.10
    max_concurrent: int = 5
    min_position_usd: float = 0.50  # min order size (use 0.50 for small bankrolls)
    daily_loss_limit: float = -999_999.0  # effectively off; set e.g. -200 to cap daily loss
    max_drawdown_pct: float = 1.0

    # Strategy thresholds
    min_spread_pct: float = 2.0
    min_edge_pct: float = 2.0
    min_confidence: float = 0.60
    min_entry_price: float = 0.05  # skip YES/NO below this (avoids 1¢ lottery bets that often go -100%)
    kelly_fraction: float = 0.25
    slippage_bps: float = 50.0
    early_exit_profit_pct: float = 0.80  # close when unrealized profit >= this % of cost (0 = disabled)
    early_exit_loss_pct: float = 0.50  # close when unrealized loss >= this % of cost (0 = disabled)
    reversal_on_loss_cap: bool = True  # after cut-loss, place one bet on opposite side in same cycle
    reversal_bet_fraction: float = 0.75  # size reversal bet as this fraction of closed position cost

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
    kalshi_live_max_order_usd: float = 10.0  # cap per order when live (e.g. $5 for $20 bankroll)

    # Storage
    db_path: str = "data/trades.db"

    # Web dashboard
    web_port: int = 8080

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
