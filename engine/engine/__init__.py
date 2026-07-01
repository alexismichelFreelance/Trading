"""es-engine — event-driven, broker/feed-agnostic ES futures trading engine.

The same Strategy code runs in backtest and live; only the Feed/Broker adapter
changes. See docs and the plan for architecture. Core speaks one normalized
event language and knows nothing about brokers or feeds.
"""
__version__ = "0.1.0"
