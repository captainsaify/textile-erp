"""All ORM models. Importing this package registers every table on
`Base.metadata` -- alembic's env.py and the schema tests rely on that,
so every new model module MUST be imported here."""

from backend.models.accounting import (
    BankLedger,
    CashLedger,
    Expense,
    Income,
    Journal,
    JournalLine,
    PartnerCapital,
)
from backend.models.base import Base
from backend.models.catalog import (
    Brand,
    Product,
    ProductCategory,
    ProductType,
    Unit,
    Warehouse,
)
from backend.models.core import Organization, Partner, User
from backend.models.inventory import Inventory, InventoryMovement
from backend.models.ocr import OcrLearningDictionary, OcrTemplate
from backend.models.parties import Customer, Supplier
from backend.models.purchases import PurchaseHeader, PurchaseLine
from backend.models.sales import SalesHeader, SalesLine
from backend.models.system import Attachment, AuditLog, Setting, WhatsappSession

__all__ = [
    "AuditLog",
    "Attachment",
    "BankLedger",
    "Base",
    "Brand",
    "CashLedger",
    "Customer",
    "Expense",
    "Income",
    "Inventory",
    "InventoryMovement",
    "Journal",
    "JournalLine",
    "OcrLearningDictionary",
    "OcrTemplate",
    "Organization",
    "Partner",
    "PartnerCapital",
    "Product",
    "ProductCategory",
    "ProductType",
    "PurchaseHeader",
    "PurchaseLine",
    "SalesHeader",
    "SalesLine",
    "Setting",
    "Supplier",
    "Unit",
    "User",
    "Warehouse",
    "WhatsappSession",
]
