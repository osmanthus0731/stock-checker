"""Validated, self-contained purchase order snapshots. Money is never a float."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

COMPANY = {
    "name": "Mizitco Sdn. Bhd.", "registration": "655190-D",
    "address": "26, Lorong BLM 1/4, Bandar Laguna Merbok, 08000 Sungai Petani, Kedah, Malaysia.",
    "telephone": "04-4418600", "fax": "04-4418601", "email": "mizitco@gmail.com",
}
TIERS = {"S12": "S12", "S100": "S100", "S500": "S500", "1K": "1K",
         "3K": "3K", "5K": "5K", "10K": "10K", "S1000": "1K",
         "S3000": "3K", "S5000": "5K", "S10000": "10K"}
CENT = Decimal("0.01")


class ValidationError(ValueError):
    pass


def text(value, label, maximum=200, required=False):
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text.")
    value = value.strip()
    if len(value) > maximum or (required and not value):
        raise ValidationError(f"{label} is required and must be at most {maximum} characters." if required
                              else f"{label} must be at most {maximum} characters.")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValidationError(f"{label} contains invalid characters.")
    return value


def decimal_value(value, label, places, maximum="999999999", positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValidationError(f"{label} must be a decimal string.")
    raw = str(value)
    if len(raw) > 30 or not re.fullmatch(r"\d+(?:\.\d{1," + str(places) + r"})?", raw):
        raise ValidationError(f"{label} must be non-negative with at most {places} decimal places.")
    result = Decimal(raw)
    if result > Decimal(maximum) or (positive and result <= 0):
        raise ValidationError(f"{label} must be {'greater than zero and ' if positive else ''}at most {maximum}.")
    return result


def date_value(value, label, required=False):
    value = text(value, label, 10, required)
    if value:
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError()
        except ValueError:
            raise ValidationError(f"{label} must use YYYY-MM-DD.") from None
    return value


def snapshot(payload, currencies):
    if not isinstance(payload, dict):
        raise ValidationError("Expected a purchase order object.")
    supplier = payload.get("supplier")
    if not isinstance(supplier, dict):
        raise ValidationError("Supplier details are required.")
    currency = text(payload.get("currency", ""), "Currency", 3, True).upper()
    if currency not in currencies:
        raise ValidationError("Select a supported currency.")
    result = {
        "schema_version": 1, "layout_version": 1, "company": deepcopy(COMPANY),
        "supplier": {k: text(supplier.get(k, ""), f"Supplier {k}", limit, k == "name")
                     for k, limit in (("name", 160), ("details", 600), ("attention", 100), ("fax", 50))},
        "date": date_value(payload.get("date", ""), "Order date", True),
        "delivery_date": date_value(payload.get("delivery_date", ""), "Delivery date"),
        "terms": text(payload.get("terms", ""), "Terms", 100),
        "replacement": text(payload.get("replacement", ""), "Replacement", 80),
        "currency": currency,
        "legacy_number": text(payload.get("legacy_number", ""), "Legacy PO number", 50),
        "items": [],
    }
    rows = payload.get("items")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
        raise ValidationError("Add between 1 and 200 items.")
    subtotal = Decimal(0)
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValidationError(f"Item {index} must be an object.")
        item = {k: text(row.get(k, ""), f"Item {index}: {k}", limit, k == "description")
                for k, limit in (("description", 600), ("product_id", 120), ("size", 100),
                                 ("mssid", 120), ("material", 100), ("notes", 1000), ("price_tier", 10))}
        if item["price_tier"] and item["price_tier"] not in set(TIERS.values()):
            raise ValidationError(f"Item {index}: invalid price tier.")
        qty = decimal_value(row.get("quantity"), f"Item {index}: quantity", 4, "99999999", True)
        price = decimal_value(row.get("unit_price"), f"Item {index}: unit price", 4, "99999999")
        amount = (qty * price).quantize(CENT, rounding=ROUND_HALF_UP)
        item.update(quantity=format(qty, "f"), unit_price=format(price, ".4f"), amount=format(amount, ".2f"))
        result["items"].append(item)
        subtotal += amount
    kind = payload.get("discount_type", "amount")
    if kind not in ("amount", "percent"):
        raise ValidationError("Discount must be an amount or percentage.")
    value = decimal_value(payload.get("discount_value", "0"), "Discount", 2,
                          "100" if kind == "percent" else "9999999999999999999")
    discount = (subtotal * value / 100 if kind == "percent" else value).quantize(CENT, rounding=ROUND_HALF_UP)
    if discount > subtotal:
        raise ValidationError("Discount cannot exceed the subtotal.")
    result.update(subtotal=format(subtotal, ".2f"), discount_type=kind,
                  discount_value=format(value, ".2f"), discount=format(discount, ".2f"),
                  total=format(subtotal - discount, ".2f"))
    return result


def product_snapshot(doc):
    def pick(*keys):
        return next((str(doc[k]) for k in keys if doc.get(k) is not None and str(doc[k]).strip()), "")
    return {"product_id": pick("uid", "Part_id"), "description": pick("name", "Desc"),
            "mssid": pick("readable_id", "Mssid"), "size": pick("size", "Size"),
            "material": pick("material", "Material", "Matl")}


def purchasing_options(rows, candidates, currency, on_date):
    """Keep record identity/currency/date; never merge P and S or guess missing currency."""
    output = []
    for row in rows:
        if (row.get("pricecd") or row.get("priced") or "").upper() != "P":
            continue
        if row.get("part_id") not in candidates or str(row.get("currency", "")).upper() != currency:
            continue
        effective = row.get("eff_date")
        if isinstance(effective, datetime):
            effective = effective.date().isoformat()
        elif isinstance(effective, date):
            effective = effective.isoformat()
        elif effective:
            try:
                effective = date.fromisoformat(str(effective)[:10]).isoformat()
            except ValueError:
                continue
        if effective and effective > on_date:
            continue
        for key, tier in TIERS.items():
            raw = row.get(key)
            if raw is None:
                continue
            try:
                price = Decimal(str(raw))
                if not price.is_finite() or price < 0 or price > Decimal("99999999"):
                    continue
                price = price.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            except InvalidOperation:
                continue
            output.append({"tier": tier, "unit_price": format(price, ".4f"), "currency": currency,
                           "customer": str(row.get("customer") or ""), "effective_date": effective or "",
                           "part_id": row["part_id"], "record_id": str(row.get("_id", ""))})
    return sorted(output, key=lambda r: r["effective_date"], reverse=True)


def utc_now():
    return datetime.now(timezone.utc).isoformat()
