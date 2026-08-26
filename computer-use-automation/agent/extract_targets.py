"""
Small domain adapter: text fields worth *extracting* (savings balance, new
sub-account id, etc.) usually aren't "interactive elements" in the
accessibility-tree sense -- they're just table cells. Rather than have the
agent hallucinate a selector for them, we give discovery a short lookup
table from the natural-language name the LLM asks to extract to a concrete,
stable locator on this app's known screens.
"""
from .schema import Locator

EXTRACT_TARGET_LOCATORS = {
    "savings balance": Locator(strategy="css", value="#savings-balance-value"),
    "new sub-account id": Locator(strategy="css", value="#new-sub-account-id"),
}


def resolve_extract_target(name: str) -> Locator:
    key = name.strip().lower()
    if key in EXTRACT_TARGET_LOCATORS:
        return EXTRACT_TARGET_LOCATORS[key]
    return Locator(strategy="text", value=name)