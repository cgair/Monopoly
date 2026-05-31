"""Futures-specific layer for Monopoly: risk gating and order execution.

Crypto-mode pipeline ends here:
``PM (FuturesDecision) → risk_gate → executor``

Decoupled from the upstream stock-mode pipeline so the rest of
TradingAgents remains usable for non-crypto workflows.
"""
