"""Stock queries -- reads the inventory cache (docs/03_Inventory.md §1)
and product catalog; powers `stock`, `stock CODE`, `stock low`,
`stock negative`, `search`."""

from __future__ import annotations

import dataclasses
import decimal
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Customer, InventoryMovement, Product, Supplier
from backend.repositories.inventory_repository import (
    InventoryRepository,
    LowStockRow,
    StockTotals,
)
from backend.repositories.party_repository import CustomerRepository, SupplierRepository
from backend.repositories.product_repository import ProductRepository

ZERO = decimal.Decimal("0")


@dataclasses.dataclass(frozen=True)
class StockSummary:
    active_products: int
    totals: StockTotals


@dataclasses.dataclass(frozen=True)
class StockDetail:
    product: Product
    qty_on_hand: decimal.Decimal
    weighted_avg_cost: decimal.Decimal
    last_movement: InventoryMovement | None

    @property
    def stock_value(self) -> decimal.Decimal:
        return self.qty_on_hand * self.weighted_avg_cost


@dataclasses.dataclass(frozen=True)
class SearchResults:
    products: list[tuple[Product, decimal.Decimal]]  # with qty on hand
    suppliers: list[Supplier]
    customers: list[Customer]

    @property
    def is_empty(self) -> bool:
        return not (self.products or self.suppliers or self.customers)


class StockService:
    def __init__(self, session: AsyncSession) -> None:
        self._products = ProductRepository(session)
        self._inventory = InventoryRepository(session)
        self._suppliers = SupplierRepository(session)
        self._customers = CustomerRepository(session)

    async def summary(self, org_id: uuid.UUID) -> StockSummary:
        return StockSummary(
            active_products=await self._products.count_active(org_id),
            totals=await self._inventory.totals(org_id),
        )

    async def details(self, org_id: uuid.UUID, code: str) -> list[StockDetail]:
        """Every product carrying this code -- more than one when brands
        share it. The caller shows them all rather than picking, since
        which brand was meant isn't knowable from the code alone."""
        details: list[StockDetail] = []
        for product in await self._products.list_by_code(org_id, code):
            inventory = await self._inventory.get_for_product(org_id, product.id)
            details.append(
                StockDetail(
                    product=product,
                    qty_on_hand=inventory.qty_on_hand if inventory else ZERO,
                    weighted_avg_cost=inventory.weighted_avg_cost if inventory else ZERO,
                    last_movement=await self._inventory.last_movement(org_id, product.id),
                )
            )
        return details

    async def suggest_codes(self, org_id: uuid.UUID, query: str) -> list[str]:
        return [p.code for p in await self._products.search(org_id, query, limit=3)]

    async def low_stock(
        self, org_id: uuid.UUID, *, negative_only: bool = False
    ) -> list[LowStockRow]:
        return await self._inventory.low_stock_rows(org_id, negative_only=negative_only)

    async def search(self, org_id: uuid.UUID, query: str) -> SearchResults:
        products = await self._products.search(org_id, query)
        with_qty: list[tuple[Product, decimal.Decimal]] = []
        for product in products:
            inventory = await self._inventory.get_for_product(org_id, product.id)
            with_qty.append((product, inventory.qty_on_hand if inventory else ZERO))
        return SearchResults(
            products=with_qty,
            suppliers=await self._suppliers.search(org_id, query),
            customers=await self._customers.search(org_id, query),
        )
