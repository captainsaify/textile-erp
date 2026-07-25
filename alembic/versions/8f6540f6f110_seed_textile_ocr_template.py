"""seed textile ocr template

The default column mapping for the textile product type, matching the
reference sheets exactly -- docs/07_OCR.md §5. supplier_id NULL means
"default template for this product type"; supplier-specific overrides
are added later as their own rows.

Revision ID: 8f6540f6f110
Revises: 7cb2a37b2a4f
Create Date: 2026-07-25 18:10:00.000000

"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f6540f6f110"
down_revision: Union[str, Sequence[str], None] = "7cb2a37b2a4f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ORG_ID = "00000000-0000-4000-a000-000000000001"
TEXTILE_PRODUCT_TYPE_ID = "00000000-0000-4000-a000-000000000201"
TEMPLATE_ID = "00000000-0000-4000-a000-000000000401"

COLUMN_MAPPING = [
    {"field": "ignore", "header_aliases": ["s.no", "sno", "sr.no", "#"]},
    {"field": "qty", "header_aliases": ["qty", "quantity", "qnty"]},
    {
        "field": "description",
        "header_aliases": ["description", "desc", "item", "particulars"],
    },
    {"field": "code", "header_aliases": ["code", "item code", "design"]},
    {"field": "ignore", "header_aliases": ["label"]},
    {"field": "weight_kg", "header_aliases": ["kg", "wt", "weight"]},
    {
        "field": "total_weight_kg",
        "header_aliases": ["t.kg", "total kg", "tot kg", "total weight"],
    },
    {"field": "ignore", "header_aliases": ["total", "amount", "value"]},
]


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO ocr_templates (id, org_id, product_type_id, supplier_id, name, "
            "column_mapping) VALUES (CAST(:id AS uuid), CAST(:org_id AS uuid), "
            "CAST(:type_id AS uuid), NULL, :name, CAST(:mapping AS jsonb))"
        ).bindparams(
            id=TEMPLATE_ID,
            org_id=ORG_ID,
            type_id=TEXTILE_PRODUCT_TYPE_ID,
            name="Textile default",
            mapping=json.dumps(COLUMN_MAPPING),
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM ocr_templates WHERE id = CAST(:id AS uuid)").bindparams(
            id=TEMPLATE_ID
        )
    )
