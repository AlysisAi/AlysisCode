"""Distinct prompt renderings for delivery tests; aliases have separate unit coverage."""

from alysis_code.prompt_guidance_catalog import (
    GUIDANCE_PROFILES,
    PROMPT_FAMILIES,
    profile_family,
    profile_variant,
)

# The three saved general densities have their own rendering path. Keep them
# alongside every canonical family/variant, including general-normal/expanded.
DISTINCT_PARENT_PROFILES = (
    "compact",
    "balanced",
    "expanded",
    *(f"{family}-{variant}" for family in PROMPT_FAMILIES for variant in ("normal", "expanded")),
)
PROFILE_ALIASES = tuple(
    (profile, f"{profile_family(profile)}-{profile_variant(profile)}")
    for profile in GUIDANCE_PROFILES
    if profile not in DISTINCT_PARENT_PROFILES
)
