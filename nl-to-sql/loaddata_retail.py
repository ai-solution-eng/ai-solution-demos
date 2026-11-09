import sqlite3
import psycopg2
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# Database configuration
db_config = {
    'user': 'postgres',
    'password': 'YOURPASSWORD',
    'host': 'postgresql.postgres.svc.cluster.local',
    'port': 5432
}
db_name = 'retail'

# Step 1: Connect to PostgreSQL server and create database
pg_conn = psycopg2.connect(
    dbname='postgres',
    user=db_config['user'],
    password=db_config['password'],
    host=db_config['host'],
    port=db_config['port']
)
pg_conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
pg_cur = pg_conn.cursor()

try:
    pg_cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    print(f"Database '{db_name}' created successfully.")
except psycopg2.errors.DuplicateDatabase:
    print(f"Database '{db_name}' already exists.")

pg_cur.close()
pg_conn.close()

# Step 2: Connect to the new database and create tables
pg_conn = psycopg2.connect(
    dbname=db_name,
    user=db_config['user'],
    password=db_config['password'],
    host=db_config['host'],
    port=db_config['port']
)
pg_cur = pg_conn.cursor()

pg_cur.execute("""
CREATE TABLE IF NOT EXISTS stores (
    store_id INTEGER PRIMARY KEY,
    store_name VARCHAR(100) NOT NULL,
    city VARCHAR(100) NOT NULL,
    region VARCHAR(50) NOT NULL
);
""")

pg_cur.execute("""
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY,
    product_name VARCHAR(100) NOT NULL,
    category VARCHAR(50) NOT NULL,
    unit_cost REAL NOT NULL,
    list_price REAL NOT NULL
);
""")

pg_cur.execute("""
CREATE TABLE IF NOT EXISTS sales (
    sale_id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES stores(store_id),
    sale_timestamp TIMESTAMP NOT NULL,
    payment_method VARCHAR(30) NOT NULL
);
""")

pg_cur.execute("""
CREATE TABLE IF NOT EXISTS sale_items (
    sale_item_id INTEGER PRIMARY KEY,
    sale_id INTEGER NOT NULL REFERENCES sales(sale_id),
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    discount_percent REAL NOT NULL
);
""")

pg_conn.commit()
print("PostgreSQL tables created.")

# Step 3: Load data from the retail SQLite database
sqlite_conn = sqlite3.connect("retail_example_data.db")
sqlite_cur = sqlite_conn.cursor()

# Transfer stores and products before their dependent sales data.
for table_name, columns in (
    ("stores", "store_id, store_name, city, region"),
    ("products", "product_id, product_name, category, unit_cost, list_price"),
    ("sales", "sale_id, store_id, sale_timestamp, payment_method"),
    ("sale_items", "sale_item_id, sale_id, product_id, quantity, unit_price, discount_percent"),
):
    print(f"Loading {table_name}...")
    sqlite_cur.execute(f"SELECT {columns} FROM {table_name}")
    for row in sqlite_cur.fetchall():
        placeholders = ", ".join(["%s"] * len(row))
        pg_cur.execute(
            f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
            row,
        )

pg_conn.commit()
sqlite_conn.close()
pg_conn.close()

print("Retail data loaded into PostgreSQL successfully.")