"""Shared input rules for Shop creation and authoritative Prestige listings.

No persistence or currency mutation here. Numeric strings support HTML forms;
booleans, floats, containers and values outside SQLite's integer range do not.
"""
import re


# The five admin-facing product types (finalized): Item -> internal `custom`,
# Color -> internal `color`, plus Potion, Title and Prestige. This is the
# canonical "types the create form offers" list the template and tests refer to.
# The validator STILL accepts the legacy `role`/`temp_role`/`xp_boost` values so
# EXISTING products remain editable; new XP Boost creation is refused at the add
# endpoint (dashboard/api/economy_shop.py), and role/temp_role are simply no
# longer offered by the create UI. Stored `type` values are never renamed.
PUBLIC_SHOP_TYPES = ("custom", "color", "potion", "title", "prestige")


class ShopValidationError(ValueError):
    pass


def integer(value, field, *, minimum=0, maximum=2**63 - 1):
    if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
        try:
            value = int(value.strip())
        except ValueError:
            raise ShopValidationError(f"{field} must be an integer.") from None
    if type(value) is not int or not minimum <= value <= maximum:
        raise ShopValidationError(f"{field} must be an integer between {minimum} and {maximum}.")
    return value


def text(value, field, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ShopValidationError(f"{field} must be {'a non-empty' if required else 'a'} string.")
    return value.strip()


def prestige_terms(price, tier, diamonds=None):
    """I–V retain positive Coin pricing; VI is explicitly free, never paid."""
    tier = integer(tier, "Prestige tier", minimum=1, maximum=6)
    price = integer(price, "Prestige VI price (Free)" if tier == 6 else "Prestige price",
                    minimum=0 if tier == 6 else 1, maximum=0 if tier == 6 else 2**63 - 1)
    if diamonds not in (None, ""):
        diamonds = integer(diamonds, "Diamond price")
        if diamonds:
            raise ShopValidationError("Prestige items cannot have a diamond price.")
    return price, tier


def listing_stock(max_stock, current_stock):
    """None/None is unlimited. Finite stock must be valid and available."""
    if max_stock is not None:
        max_stock = integer(max_stock, "Maximum stock", minimum=1)
    if current_stock is None:
        if max_stock is not None:
            raise ShopValidationError("Finite stock is missing its current quantity.")
        return None
    current_stock = integer(current_stock, "Current stock")
    if max_stock is not None and current_stock > max_stock:
        raise ShopValidationError("Current stock exceeds maximum stock.")
    if current_stock == 0:
        raise ShopValidationError("This item is out of stock.")
    return current_stock


def shop_input(data):
    """Validate before any Shop/catalog write; return normalized API/form fields."""
    import math
    if not isinstance(data, dict):
        raise ShopValidationError("A Shop item object is required.")
    result = dict(data)
    result["name"] = text(data.get("name"), "Name", required=True)
    option_parent = data.get("option_of_id")
    result["option_of_id"] = (
        None if option_parent in (None, "")
        else integer(option_parent, "Option parent ID", minimum=1))
    kind = text(data.get("type"), "Item type", required=True)
    # `color` is the fifth admin-facing product type (Item/Color/Potion/Title/
    # Prestige). It is NOT a new reward type: cogs/shop.py already resolves a
    # Colour purchase to the existing role grant (permanent) or timed-role
    # holding (positive duration) under CATEGORY_COLOR — so a Colour listing
    # carries a role_id exactly like `role`/`temp_role` and needs no purchase
    # -engine change. Legacy `role`/`temp_role`/`xp_boost` stay valid here so
    # EXISTING products remain editable; new XP Boost creation is refused at
    # the add endpoint (dashboard/api/economy_shop.py), not in this shared
    # validator, so a legacy boost can still have its safe fields edited.
    if kind not in {"role", "temp_role", "xp_boost", "prestige", "potion", "title", "custom", "color"}:
        raise ShopValidationError("Invalid Shop item type.")
    result["type"] = kind
    vi = kind == "prestige" and integer(data.get("prestige_tier"), "Prestige tier", minimum=1, maximum=6) == 6
    result["price"] = integer(data.get("price"), "Price")
    for field in ("description", "icon_url", "rarity"):
        result[field] = text(data.get(field), field)
    for field in ("max_stock", "price_diamonds", "role_id", "required_role_id", "duration_hours"):
        if vi and field in {"max_stock", "required_role_id", "duration_hours", "role_id"}:
            result[field] = None
            continue
        value = data.get(field)
        # Existing form convention: blank/zero maximum means unlimited stock.
        result[field] = None if value in (None, "") else integer(value, field)
        if result[field] == 0:
            result[field] = None
    result["featured"] = integer(data.get("featured", 0), "Featured", maximum=1)
    result["required_level"] = 0 if vi else integer(data.get("required_level", 0), "Required level")
    tier = data.get("prestige_tier")
    if kind == "prestige":
        result["price"], tier = prestige_terms(result["price"], tier, result["price_diamonds"])
    else:
        tier = None if tier in (None, "", 0, "0") else integer(tier, "Prestige tier", minimum=1, maximum=6)
    result["prestige_tier"] = tier
    # Free rule (normal items): an explicit coin price of 0 with no diamond
    # price is a legitimate Free listing — the same zero-charge meaning
    # utils/shop_publisher.is_free() derives, and the same 0 that Prestige VI
    # already stores. Nothing to reject here: `integer(..., "Price")` above
    # already refuses None/missing, negative, fractional, boolean and
    # malformed input, and a price_diamonds > 0 still makes the item paid in
    # Diamonds (precedence unchanged). Prestige keeps its own terms: I–V stay
    # >= 1, VI stays exactly 0 via prestige_terms().
    multiplier = data.get("xp_boost_multiplier")
    if multiplier in (None, ""):
        multiplier = None
    else:
        try:
            if isinstance(multiplier, bool):
                raise ValueError
            multiplier = float(multiplier)
            if not math.isfinite(multiplier) or multiplier < 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise ShopValidationError("Effect multiplier must be a finite nonnegative number.") from None
    result["xp_boost_multiplier"] = multiplier
    if kind in {"potion", "xp_boost"}:
        if multiplier is None or multiplier <= 1:
            raise ShopValidationError("Effect multiplier must be greater than 1.")
        if result["duration_hours"] is None:
            raise ShopValidationError("Effect duration must be positive.")
    if kind in {"role", "temp_role", "color"} and result["role_id"] is None:
        raise ShopValidationError("A role item requires a role ID.")
    if kind == "temp_role" and result["duration_hours"] is None:
        raise ShopValidationError("A temporary role requires a positive duration.")
    # Finalized type rules: Title and Prestige are permanent grants and may NOT
    # carry a duration. A submitted positive duration is REJECTED (not silently
    # zeroed) so the rule is explicit and an unfinished form can't smuggle a
    # timed Title/Prestige through. A duration of 0 (or omitted) is normalized
    # to None above and accepted as "permanent". Prestige VI already had its
    # duration forced to None by the unmetered branch, so it never trips this.
    # Item (custom) and Color KEEP duration semantics (0/None = permanent,
    # positive = timed holding / timed role) and Potion keeps its effect
    # duration (validated above).
    if kind in {"title", "prestige"} and result["duration_hours"] is not None:
        raise ShopValidationError(
            "Title and Prestige products are permanent and cannot have a duration.")
    return result
