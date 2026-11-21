"""Alembic's environment. `db.migrate` hands it the connection to migrate on, inside the write lock."""

from alembic import context

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=None)

with context.begin_transaction():
    context.run_migrations()
