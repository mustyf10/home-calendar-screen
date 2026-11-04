"""Breakfast category and ingredient-based recipe assistance.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("dishes", recreate="always") as batch:
        batch.drop_constraint("ck_dish_type", type_="check")
        batch.create_check_constraint(
            "ck_dish_type", "type IN ('breakfast', 'dinner', 'snack', 'dessert', 'baking', 'side')"
        )
        batch.add_column(sa.Column("nutrition", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("generated_recipe", sa.JSON(), nullable=True))


def downgrade() -> None:
    # The previous schema cannot represent Breakfast or retain AI assistance data.
    op.execute("UPDATE dishes SET type = 'dinner' WHERE type = 'breakfast'")
    with op.batch_alter_table("dishes", recreate="always") as batch:
        batch.drop_constraint("ck_dish_type", type_="check")
        batch.create_check_constraint(
            "ck_dish_type", "type IN ('dinner', 'snack', 'dessert', 'baking', 'side')"
        )
        batch.drop_column("generated_recipe")
        batch.drop_column("nutrition")
