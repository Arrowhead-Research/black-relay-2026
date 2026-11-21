"""The schema: sites and their nodes, what each node last said (placed or not), the emitters an admin allowed, and
what the Fusion poller keeps.

Nothing was deployed before this, so it is the first revision: earlier dev revisions (trip detail, the trip's
emitter, the allow list) were squashed into it. db.py's models describe the same tables (tests/test_db.py checks
the two agree).
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "sites",
        sa.Column("site_id", sa.String, primary_key=True),
        sa.Column("lat", sa.Float, nullable=False),
        sa.Column("lon", sa.Float, nullable=False),
        sa.Column("boundary", sa.Text, nullable=True),
        sa.Column("mission_package_name", sa.String, nullable=False),
        sa.Column("mission_package_hash", sa.String, nullable=False),
    )
    op.create_table(
        "nodes",
        sa.Column("site_id", sa.String, sa.ForeignKey("sites.site_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("subsystem", sa.String, primary_key=True),
        sa.Column("node_id", sa.String, primary_key=True),
        sa.Column("lat", sa.Float, nullable=False),
        sa.Column("lon", sa.Float, nullable=False),
        sa.Column("range_m", sa.Float, nullable=True),
        sa.Column("accuracy_m", sa.Float, nullable=True),
        sa.Column("stream", sa.String, nullable=True),
        sa.Column("watch_lat", sa.Float, nullable=True),
        sa.Column("watch_lon", sa.Float, nullable=True),
        sa.Column("fusion_seen_at", sa.DateTime, nullable=True),
    )
    op.create_table(
        "node_state",
        sa.Column("site_id", sa.String, primary_key=True),
        sa.Column("subsystem", sa.String, primary_key=True),
        sa.Column("node_id", sa.String, primary_key=True),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("updated", sa.DateTime, nullable=False),
        sa.Column("tripped_at", sa.DateTime, nullable=True),
        sa.Column("threat_lat", sa.Float, nullable=True),
        sa.Column("threat_lon", sa.Float, nullable=True),
        sa.Column("threat_ce", sa.Float, nullable=True),
        sa.Column("severity", sa.Integer, nullable=True),
        sa.Column("event_type", sa.String, nullable=True),
        sa.Column("detail", sa.String, nullable=True),
        sa.Column("threat_expires_at", sa.DateTime, nullable=True),
        sa.Column("threat_delete_rounds", sa.Integer, nullable=False, server_default="0"),
        # RF: the emitter that caused the trip (a MAC address) and where Fusion last put it.
        sa.Column("emitter", sa.String, nullable=True),
        sa.Column("emitter_x_ft", sa.Float, nullable=True),
        sa.Column("emitter_y_ft", sa.Float, nullable=True),
        sa.ForeignKeyConstraint(
            ["site_id", "subsystem", "node_id"],
            ["nodes.site_id", "nodes.subsystem", "nodes.node_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "unplaced_state",
        sa.Column("site_id", sa.String, sa.ForeignKey("sites.site_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("subsystem", sa.String, primary_key=True),
        sa.Column("node_id", sa.String, primary_key=True),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("updated", sa.DateTime, nullable=False),
        sa.Column("tripped_at", sa.DateTime, nullable=True),
        sa.Column("severity", sa.Integer, nullable=True),
        sa.Column("event_type", sa.String, nullable=True),
        sa.Column("detail", sa.String, nullable=True),
    )
    op.create_table(
        "fusion_discovered",
        sa.Column("site_id", sa.String, primary_key=True),
        sa.Column("subsystem", sa.String, primary_key=True),
        sa.Column("node_id", sa.String, primary_key=True),
        sa.Column("first_seen", sa.DateTime, nullable=False),
        sa.Column("last_seen", sa.DateTime, nullable=False),
        sa.Column("event_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_type", sa.String, nullable=True),
    )
    op.create_table(
        "fusion_held",
        sa.Column("event_id", sa.String, primary_key=True),
        sa.Column("site_id", sa.String, nullable=False),
        sa.Column("subsystem", sa.String, nullable=False),
        sa.Column("node_id", sa.String, nullable=False),
        sa.Column("type", sa.String, nullable=False),
        sa.Column("event_timestamp", sa.DateTime, nullable=False),
        sa.Column("severity", sa.Integer, nullable=True),
        sa.Column("detail", sa.String, nullable=True),
        sa.Column("emitter", sa.String, nullable=True),
    )
    op.create_index("ix_fusion_held_node", "fusion_held", ["site_id", "subsystem", "node_id"])
    op.create_table(
        "fusion_sources",
        sa.Column("name", sa.String, primary_key=True),
        sa.Column("cursor", sa.Integer, nullable=True),
        sa.Column("last_event_id", sa.String, nullable=True),
        sa.Column("last_poll_ok_at", sa.DateTime, nullable=True),
        sa.Column("last_error_code", sa.String, nullable=True),
    )
    op.create_table(
        "fusion_gaps",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("from_cursor", sa.Integer, nullable=True),
        sa.Column("resume_after", sa.Integer, nullable=False),
        sa.Column("at", sa.DateTime, nullable=False),
    )
    # The last position Fusion gave for each emitter at a site.
    op.create_table(
        "fusion_emitters",
        sa.Column("site_id", sa.String, primary_key=True),
        sa.Column("emitter", sa.String, primary_key=True),
        sa.Column("x_ft", sa.Float, nullable=False),
        sa.Column("y_ft", sa.Float, nullable=False),
        sa.Column("heard_at", sa.DateTime, nullable=False),
    )
    # The emitters an admin has allowed at a site: an RF alert one of them causes there trips nothing.
    op.create_table(
        "allowed_emitters",
        sa.Column("site_id", sa.String, sa.ForeignKey("sites.site_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("emitter", sa.String, primary_key=True),
        sa.Column("name", sa.String, nullable=True),
        sa.Column("added_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    for table in (
        "allowed_emitters",
        "fusion_emitters",
        "fusion_gaps",
        "fusion_sources",
        "fusion_held",
        "fusion_discovered",
        "unplaced_state",
        "node_state",
        "nodes",
        "sites",
    ):
        op.drop_table(table)
