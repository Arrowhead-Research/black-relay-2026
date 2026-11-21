"""The one migration and db.py's models describe the same schema: a database migrated to head has exactly the
tables, columns, keys and indexes the models do."""

import sqlalchemy as sa

from pytak_gateway import db


def _shape(engine: sa.Engine) -> dict:
    i = sa.inspect(engine)
    out = {}
    for t in i.get_table_names():
        if t == "alembic_version":
            continue
        out[t] = {
            "columns": {c["name"]: (str(c["type"]), c["nullable"]) for c in i.get_columns(t)},
            "primary key": tuple(i.get_pk_constraint(t)["constrained_columns"]),
            "foreign keys": sorted(
                (tuple(f["constrained_columns"]), f["referred_table"], f.get("options", {}).get("ondelete"))
                for f in i.get_foreign_keys(t)
            ),
            "indexes": sorted((x["name"], tuple(x["column_names"])) for x in i.get_indexes(t)),
        }
    return out


def test_the_migration_makes_the_schema_the_models_describe(tmp_path):
    migrated = sa.create_engine(f"sqlite:///{tmp_path / 'gateway.db'}")
    db.migrate(migrated)
    modelled = sa.create_engine("sqlite://")
    db.Base.metadata.create_all(modelled)
    assert _shape(migrated) == _shape(modelled)


def test_there_is_one_migration():
    migrated = sa.create_engine("sqlite://")
    db.migrate(migrated)
    with migrated.connect() as c:
        assert c.execute(sa.text("select version_num from alembic_version")).scalar() == "0001"
