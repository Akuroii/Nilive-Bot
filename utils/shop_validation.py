"""Shared input rules for Shop creation and authoritative Prestige listings.

No persistence or currency mutation here. Numeric strings support HTML forms;
booleans, floats, containers and values outside SQLite's integer range do not.
"""
import re


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
    kind = text(data.get("type"), "Item type", required=True)
    if kind not in {"role", "temp_role", "xp_boost", "prestige", "potion", "title", "custom"}:
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
    if kind != "prestige" and not (result["price_diamonds"] or result["price"]):
        raise ShopValidationError("Paid Shop items require a positive price.")
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
    if kind in {"role", "temp_role"} and result["role_id"] is None:
        raise ShopValidationError("A role item requires a role ID.")
    if kind == "temp_role" and result["duration_hours"] is None:
        raise ShopValidationError("A temporary role requires a positive duration.")
    return result
