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
    daily_loss_limit: float = -200.0
    max_drawdown_pct: float = 0.05

    # Strategy thresholds
    min_spread_pct: float = 2.0
    min_edge_pct: float = 2.0
    min_confidence: float = 0.60
    kelly_fraction: float = 0.25
    slippage_bps: float = 50.0

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

    # Storage
    db_path: str = "data/trades.db"

    # Web dashboard
    web_port: int = 8080

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
