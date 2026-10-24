from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid',allow_inf_nan=False)

class AuthInput(StrictModel):
    password: str = Field(min_length=1, max_length=256)
    setup_token: str | None = None
    totp_code: str | None = Field(default=None,min_length=6,max_length=6)

class PasswordInput(StrictModel):
    current_password: str = Field(min_length=1,max_length=256)
    new_password: str = Field(min_length=12,max_length=256)
    totp_code: str | None = Field(default=None,min_length=6,max_length=6)

class LegInput(StrictModel):
    instrument_id: str = Field(min_length=1, max_length=150)
    side: Literal['BUY', 'SELL']
    lots: int = Field(ge=1, le=1000)
    order_type: Literal['MARKET', 'LIMIT'] = 'MARKET'
    price: float | None = Field(default=None, gt=0, le=1e9)
    enabled: bool = True
    stop_loss: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)

class TradeInput(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    underlying: str = Field(min_length=1, max_length=60)
    brokers: list[Literal['kotak','fivepaisa']] = Field(min_length=1, max_length=2)
    legs: list[LegInput] = Field(min_length=1, max_length=20)
    idempotency_key: str = Field(min_length=8, max_length=150)
    confirmed: bool = False

    @model_validator(mode='after')
    def independent_brokers(self):
        if len(set(self.brokers)) != len(self.brokers):
            raise ValueError('Choose each broker once for separate simulations.')
        return self

class ActionInput(StrictModel):
    action: Literal['add_lots','add_leg','partial_exit','exit_leg','exit_strategy','duplicate','edit','replace_leg','cancel_pending','settle_expiry']
    idempotency_key: str = Field(min_length=8, max_length=150)
    confirmed: bool
    leg_id: str | None = None
    lots: int | None = Field(default=None, ge=1, le=1000)
    price: float | None = Field(default=None, gt=0, le=1e9)
    leg: LegInput | None = None
    name: str | None = Field(default=None, min_length=1,max_length=100)
    stop_loss: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)

class SettingsInput(StrictModel):
    trade_count_definition: Literal['strategies','executions'] | None = None
    brokerage_per_order: float | None = Field(default=None,ge=0,le=10000)
    charges_percent: float | None = Field(default=None,ge=0,le=10)
    slippage_bps: float | None = Field(default=None,ge=0,le=10000)
    fill_basis: Literal['ltp','bid_ask'] | None = None
    logging_mode: Literal['summary','every_update','one_second','full_history'] | None = None
    preferred_feed: Literal['broker','kotak','fivepaisa'] | None = None
    stale_after_seconds: int | None = Field(default=None,ge=2,le=3600)
    demo_mode: bool | None = None
    delete_after_local_save: bool | None = None
    auto_delete_online: bool | None = None
    unlimited_paper_buying_power: bool | None = None
    paper_capital: float | None = Field(default=None,ge=0,le=1e12)

class RiskLegInput(BaseModel):
    model_config=ConfigDict(extra='allow',allow_inf_nan=False)
    underlying: str = Field(min_length=1)
    expiry: str = Field(min_length=1)
    side: Literal['BUY','SELL']
    option_type: Literal['CE','PE','FUT']
    strike: float = Field(ge=0)
    lot_size: int = Field(ge=1)
    lots: int = Field(default=1,ge=0)
    open_lots: int | None = Field(default=None,ge=0)
    entry_price: float | None = Field(default=None,ge=0)
    price: float | None = Field(default=None,ge=0)
    price_multiplier: float | None = Field(default=None,gt=0)
    enabled: bool = True

    @model_validator(mode='after')
    def require_price(self):
        if self.entry_price is None and self.price is None:
            raise ValueError('Each risk leg needs an entry price.')
        return self

class RiskInput(StrictModel):
    legs: list[RiskLegInput] = Field(min_length=1,max_length=20)
    spot: float | None = Field(default=None,gt=0)
    charges: float = Field(default=0,ge=0)
