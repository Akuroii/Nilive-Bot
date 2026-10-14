"""Shim — the canonical rank-card payload aggregation lives in
utils/rank_card_data.py (the bot and the renderer both import it from
there). Kept so root-level imports of the historical path keep working."""
from utils.rank_card_data import get_rank_card_data  # noqa: F401
