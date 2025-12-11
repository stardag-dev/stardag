#!/bin/bash
# Create companion .sql file for alembic migrations
# This script is called by alembic post_write_hooks

py_file="$1"
sql_file="${py_file%.py}.sql"

if [ ! -f "$sql_file" ]; then
    echo "-- Migration SQL statements go here" > "$sql_file"
    echo "Created: $sql_file"
fi
