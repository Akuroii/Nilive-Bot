"""Shim — the canonical rank-card renderer lives in
utils/rank_card_renderer.py (the /rank command and the integration
tests both import it from there). Kept so root-level imports of the
historical path keep working, mirroring the root rank_card_data.py
shim that already fronts utils/rank_card_data.py."""
from utils.rank_card_renderer import render_rank_card  # noqa: F401
