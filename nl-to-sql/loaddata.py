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
db_name = 'manufacturing'

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
CREATE TABLE IF NOT EXISTS machines (
    machine_id VARCHAR(5) PRIMARY KEY,
    name VARCHAR(50),
    location VARCHAR(50),
    production_line INTEGER
);
""")

pg_cur.execute("""
CREATE TABLE IF NOT EXISTS operators (
    operator_id INTEGER PRIMARY KEY,
    name VARCHAR(100),
    gender VARCHAR(10),
    machine_id VARCHAR(5),
    production_line INTEGER,
    location VARCHAR(50),
    shift VARCHAR(20),
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);
""")

pg_cur.execute("""
CREATE TABLE IF NOT EXISTS machine_metrics (
    metric_id INTEGER PRIMARY KEY,
    timestamp TIMESTAMP,
    machine_id VARCHAR(5),
    operator_id INTEGER,
    operation_mode VARCHAR(10),
    temperature REAL,
    vibration REAL,
    power_consumption REAL,
    defect_rate REAL,
    units_produced INTEGER,
    planned_production_time REAL,
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id),
    FOREIGN KEY (operator_id) REFERENCES operators(operator_id)
);
""")

pg_conn.commit()
print("PostgreSQL tables created.")

# Step 3: Load data from SQLite .db file
sqlite_conn = sqlite3.connect("manufacturing_example_data.db")
sqlite_cur = sqlite_conn.cursor()

# Transfer machines
print("Loading machines...")
sqlite_cur.execute("SELECT * FROM machines")
machines_rows = sqlite_cur.fetchall()
for row in machines_rows:
    pg_cur.execute(
        "INSERT INTO machines (machine_id, name, location, production_line) VALUES (%s, %s, %s, %s) ON CONFLICT (machine_id) DO NOTHING",
        row
    )

# Transfer operators
print("Loading operators...")
sqlite_cur.execute("SELECT * FROM operators")
operators_rows = sqlite_cur.fetchall()
for row in operators_rows:
    pg_cur.execute(
        "INSERT INTO operators (operator_id, name, gender, machine_id, production_line, location, shift) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (operator_id) DO NOTHING",
        row
    )

# Transfer machine_metrics
print("Loading machine_metrics...")
sqlite_cur.execute("SELECT * FROM machine_metrics")
metrics_rows = sqlite_cur.fetchall()
for row in metrics_rows:
    pg_cur.execute(
        """INSERT INTO machine_metrics (
            metric_id, timestamp, machine_id, operator_id, operation_mode, temperature, vibration, power_consumption, defect_rate, units_produced, planned_production_time
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (metric_id) DO NOTHING""",
        row
    )

pg_conn.commit()
sqlite_conn.close()
pg_conn.close()

print("Data loaded into PostgreSQL successfully.")