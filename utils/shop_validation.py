"""Shared input rules for Shop creation and authoritative Prestige listings.

No persistence or currency mutation here. Numeric strings support HTML forms;
booleans, floats, containers and values outside SQLite's integer range do not.
"""
import re


class ShopValidationError(ValueError):
    pass


# Every internal type the backend accepts. The five choices the admin UI
# offers map onto these; the rest are legacy types that existing listings and
# existing purchase/level-claim paths still use and must keep working:
#
#   Items     -> custom   (duration 0 = permanent, > 0 = timed)
#   Colors    -> color    (duration 0 = permanent role, > 0 = timed role)
#   Potion    -> potion   (duration = effect length after activation)
#   Title     -> title    (permanent cosmetic)
#   Prestige  -> prestige (progression purchase, never an inventory holding)
#
#   role / temp_role / xp_boost  -> legacy, still accepted and still delivered
#   through their existing paths. `xp_boost` is no longer offered at creation
#   (Potion is not equivalent: a boost starts on purchase, a potion is held),
#   but nothing that already exists is broken by hiding the option.
SHOP_ITEM_TYPES = frozenset({
    "custom", "color", "potion", "title", "prestige",
    "role", "temp_role", "xp_boost",
})

# Public UI choices, in the exact order the admin sees them.
PUBLIC_SHOP_TYPES = ("custom", "color", "potion", "title", "prestige")


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
    if kind not in SHOP_ITEM_TYPES:
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
    # Prestige is a progression purchase, not a holding: a duration on it has
    # never had any effect and must not start implying one now that duration
    # means something for real products. VI already clears the field above.
    if kind == "prestige" and result["duration_hours"] is not None:
        raise ShopValidationError("Prestige is permanent and cannot have a duration.")
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
        # A Potion's duration_hours is the effect length AFTER activation, not
        # the lifetime of the stored item, so a role ID on one is meaningless.
        if kind == "potion" and result["role_id"] is not None:
            raise ShopValidationError("A potion cannot have a role ID.")
    if kind in {"role", "temp_role", "color"} and result["role_id"] is None:
        raise ShopValidationError("A role item requires a role ID.")
    if kind == "temp_role" and result["duration_hours"] is None:
        raise ShopValidationError("A temporary role requires a positive duration.")
    if kind == "title" and result.get("duration_hours") is not None:
        # Titles are cosmetic and permanent; generic timed-ownership semantics
        # are not defined for them, so a duration is refused rather than
        # silently ignored (which is what used to happen).
        raise ShopValidationError("Titles are permanent and cannot have a duration.")
    if kind == "title" and multiplier is not None:
        raise ShopValidationError("A title cannot have an effect multiplier.")
    if kind in {"custom", "color"} and multiplier is not None:
        raise ShopValidationError("Only potions and XP boosts have an effect multiplier.")
    if kind == "custom" and result["role_id"] is not None:
        # Item listings carry no Discord role; a role belongs to Colors.
        raise ShopValidationError("An Item cannot have a role ID — use Colors.")
    if kind == "color":
        # Normalized so callers compare one shape: None means permanent, a
        # positive int means the role entitlement expires.
        result["duration_hours"] = result["duration_hours"] or None
    return result
