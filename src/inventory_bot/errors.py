"""User-facing and configuration errors for the inventory bot."""


class InventoryBotError(Exception):
    """Base class for expected inventory-bot errors."""


class ConfigurationError(InventoryBotError):
    """The bot or backing sheet is not configured correctly."""


class ParseError(InventoryBotError):
    """A reservation message could not be parsed safely."""


class ItemNotFoundError(InventoryBotError):
    """No item matched the supplied name."""


class AmbiguousItemError(InventoryBotError):
    """More than one item matched the supplied name."""


class AvailabilityError(InventoryBotError):
    """The requested item is not currently available."""


class CancellationError(InventoryBotError):
    """A current reservation cannot be cancelled by this request."""


class SheetSchemaError(ConfigurationError):
    """The spreadsheet columns do not match the expected schema."""
