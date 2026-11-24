"""Product profile registry and ordered assessment steps (P5 Tasks 1, 2, 4).

Defines supported product profiles (HEALTH, VEHICLE, PROPERTY) and rejects unsupported products.
Formula definitions are tagged ILLUSTRATIVE_DEFAULTS pending insurer approval under Decision D11.
"""

from enum import StrEnum
from typing import NamedTuple

FORMULA_SOURCE: str = "ILLUSTRATIVE_DEFAULTS"
CALCULATION_SPEC_STATUS: str = "ILLUSTRATIVE"


class ProductTypeEnum(StrEnum):
    HEALTH = "HEALTH"
    VEHICLE = "VEHICLE"
    PROPERTY = "PROPERTY"


class UnsupportedProductError(ValueError):
    """Raised when an assessment is attempted for a product not in the registry."""

    pass


class ProductProfile(NamedTuple):
    product_type: ProductTypeEnum
    name: str
    ordered_steps: tuple[str, ...]
    supported_components: tuple[str, ...]
    formula_source: str = FORMULA_SOURCE


PRODUCT_REGISTRY: dict[str, ProductProfile] = {
    "HEALTH": ProductProfile(
        product_type=ProductTypeEnum.HEALTH,
        name="Health Insurance",
        ordered_steps=("DEDUCTIBLE", "COPAY", "LIMIT_CAP"),
        supported_components=(
            "ROOM_RENT",
            "ICU",
            "SURGERY",
            "MEDICINE",
            "INVESTIGATION",
            "CONSULTATION",
            "OTHER",
        ),
    ),
    "VEHICLE": ProductProfile(
        product_type=ProductTypeEnum.VEHICLE,
        name="Motor Vehicle Insurance",
        ordered_steps=("DEPRECIATION", "DEDUCTIBLE", "LIMIT_CAP"),
        supported_components=(
            "PARTS",
            "LABOUR",
            "PAINTING",
            "OTHER",
        ),
    ),
    "PROPERTY": ProductProfile(
        product_type=ProductTypeEnum.PROPERTY,
        name="Property & Casualty Insurance",
        ordered_steps=("COINSURANCE", "DEDUCTIBLE", "LIMIT_CAP"),
        supported_components=(
            "STRUCTURE",
            "CONTENTS",
            "OUTBUILDINGS",
            "OTHER",
        ),
    ),
}


def get_product_profile(product_type: str) -> ProductProfile:
    """Retrieve product profile or raise UnsupportedProductError (no generic fallback)."""
    pt_upper = product_type.upper()
    if pt_upper not in PRODUCT_REGISTRY:
        supported = ", ".join(PRODUCT_REGISTRY.keys())
        raise UnsupportedProductError(
            f"Unsupported product type '{product_type}'. Supported products are: {supported}. "
            "No generic fallback is allowed."
        )
    return PRODUCT_REGISTRY[pt_upper]
