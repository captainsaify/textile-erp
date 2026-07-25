"""seed default org, units, textile product type, main warehouse

Seed data ships as a data migration, not an application-startup side
effect -- docs/02_Database.md §6. Fixed UUIDs so downgrade() can remove
exactly what upgrade() inserted and later migrations/fixtures can
reference the rows stably.

The organization row itself is seeded here too (docs/02_Database.md
§3.1: "single seeded row today") with a neutral name -- the partners
rename it via the `settings` WhatsApp command.

Revision ID: 7cb2a37b2a4f
Revises: 1eca2cb3e208
Create Date: 2026-07-25 15:35:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7cb2a37b2a4f'
down_revision: Union[str, Sequence[str], None] = '1eca2cb3e208'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ORG_ID = "00000000-0000-4000-a000-000000000001"

UNIT_IDS = {
    "KG": "00000000-0000-4000-a000-000000000101",
    "PCS": "00000000-0000-4000-a000-000000000102",
    "MTR": "00000000-0000-4000-a000-000000000103",
    "ROLL": "00000000-0000-4000-a000-000000000104",
    "BOX": "00000000-0000-4000-a000-000000000105",
}

# docs/02_Database.md §3.4 seeded defaults
UNITS = [
    ("KG", "Kilogram", "weight"),
    ("PCS", "Pieces", "count"),
    ("MTR", "Metre", "length"),
    ("ROLL", "Roll", "count"),
    ("BOX", "Box", "count"),
]

TEXTILE_PRODUCT_TYPE_ID = "00000000-0000-4000-a000-000000000201"

# docs/02_Database.md §3.6 seed row for textile
TEXTILE_ATTRIBUTE_SCHEMA = (
    '{"type": "object", '
    '"properties": {'
    '"gsm": {"type": "number", "description": "grams per square metre"}, '
    '"width_cm": {"type": "number"}, '
    '"color": {"type": "string"}}, '
    '"additionalProperties": false}'
)

MAIN_WAREHOUSE_ID = "00000000-0000-4000-a000-000000000301"


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO organizations (id, name) VALUES (CAST(:id AS uuid), :name)"
        ).bindparams(id=ORG_ID, name="Default Organization")
    )
    for code, name, kind in UNITS:
        op.execute(
            sa.text(
                "INSERT INTO units (id, org_id, code, name, kind) "
                "VALUES (CAST(:id AS uuid), CAST(:org_id AS uuid), :code, :name, CAST(:kind AS unit_kind))"
            ).bindparams(id=UNIT_IDS[code], org_id=ORG_ID, code=code, name=name, kind=kind)
        )
    op.execute(
        sa.text(
            "INSERT INTO product_types "
            "(id, org_id, code, name, default_unit_id, attribute_schema) "
            "VALUES (CAST(:id AS uuid), CAST(:org_id AS uuid), 'textile', 'Textile / Fabric', CAST(:unit_id AS uuid), "
            "CAST(:schema AS jsonb))"
        ).bindparams(
            id=TEXTILE_PRODUCT_TYPE_ID,
            org_id=ORG_ID,
            unit_id=UNIT_IDS["KG"],
            schema=TEXTILE_ATTRIBUTE_SCHEMA,
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO warehouses (id, org_id, name, is_default) "
            "VALUES (CAST(:id AS uuid), CAST(:org_id AS uuid), 'Main', TRUE)"
        ).bindparams(id=MAIN_WAREHOUSE_ID, org_id=ORG_ID)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM warehouses WHERE id = CAST(:id AS uuid)").bindparams(id=MAIN_WAREHOUSE_ID)
    )
    op.execute(
        sa.text("DELETE FROM product_types WHERE id = CAST(:id AS uuid)").bindparams(
            id=TEXTILE_PRODUCT_TYPE_ID
        )
    )
    for unit_id in UNIT_IDS.values():
        op.execute(sa.text("DELETE FROM units WHERE id = CAST(:id AS uuid)").bindparams(id=unit_id))
    op.execute(sa.text("DELETE FROM organizations WHERE id = CAST(:id AS uuid)").bindparams(id=ORG_ID))
