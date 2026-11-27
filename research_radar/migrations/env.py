from alembic import context

with context.config.attributes["engine"].connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
