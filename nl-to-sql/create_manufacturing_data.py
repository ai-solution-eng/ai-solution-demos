import sqlite3
import random
from datetime import datetime, timedelta

conn = sqlite3.connect("manufacturing_example_data.db")
cursor = conn.cursor()

# Table: machines
cursor.execute("""
CREATE TABLE machines (
    machine_id TEXT PRIMARY KEY,
    name TEXT,
    location TEXT,
    production_line INTEGER
)
""")

# Table: operators
cursor.execute("""
CREATE TABLE operators (
    operator_id INTEGER PRIMARY KEY,
    name TEXT,
    gender TEXT,
    machine_id TEXT,
    production_line INTEGER,
    location TEXT,
    shift TEXT,
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
)
""")

# Table: machine_metrics
cursor.execute("""
CREATE TABLE machine_metrics (
    metric_id INTEGER PRIMARY KEY,
    timestamp DATETIME,
    machine_id TEXT,
    operator_id INTEGER,
    operation_mode TEXT,
    temperature REAL,
    vibration REAL,
    power_consumption REAL,
    defect_rate REAL,
    units_produced INTEGER,
    planned_production_time REAL,
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id),
    FOREIGN KEY (operator_id) REFERENCES operators(operator_id)
)
""")

locations = ['Böblingen', 'Dublin', 'Bad Tölz', 'Hamburg', 'Prague']
machine_types = ['CNC Lathe', 'Injection Molding Machine', 'Laser Cutter', 'Assembly Robot', 'Press Machine']
production_lines = list(range(1, 11))

# Generate machines
machines_data = []
machine_id_set = set()
for location in locations:
    for prod_line in production_lines[:5]:  # 5 production lines per location
        for mtype in machine_types:
            # Generate unique 5-digit machine_id
            while True:
                machine_id = f"{random.randint(0, 99999):05d}"
                if machine_id not in machine_id_set:
                    machine_id_set.add(machine_id)
                    break
            machines_data.append((machine_id, mtype, location, prod_line))

# Insert machines
cursor.executemany("""
INSERT INTO machines (machine_id, name, location, production_line)
VALUES (?, ?, ?, ?)
""", machines_data)

# Names for operators
first_names_female = ['Anna', 'Maria', 'Julia', 'Sophie', 'Laura', 'Emma', 'Lena', 'Mia', 'Lea', 'Clara']
first_names_male = ['Max', 'Paul', 'Leon', 'Lukas', 'Finn', 'Ben', 'Noah', 'Elias', 'Luis', 'Jonas']
last_names = ['Müller', 'Schmidt', 'Schneider', 'Fischer', 'Weber', 'Meyer', 'Wagner', 'Becker', 'Hoffmann', 'Schäfer']

# Irish names for Dublin
first_names_irish_female = ['Aoife', 'Ciara', 'Niamh', 'Saoirse', 'Orla', 'Aisling', 'Eimear', 'Gráinne', 'Sinéad', 'Clodagh']
first_names_irish_male = ['Conor', 'Cian', 'Darragh', 'Fionn', 'Oisin', 'Ronan', 'Eoin', 'Seán', 'Padraig', 'Cathal']
last_names_irish = ['Murphy', 'Kelly', "O'Sullivan", 'Walsh', "O'Brien", 'Ryan', "O'Connor", "O'Neill", "O'Reilly", 'Doyle']

shifts = ['Morning', 'Evening', 'Night']

# Generate operators
operators_data = []
operator_id = 1
for location in locations:
    for i in range(20):  # 20 workers per location
        gender = 'Female' if i % 2 == 0 else 'Male'
        if location == 'Dublin':
            if gender == 'Female':
                first_name = random.choice(first_names_irish_female)
            else:
                first_name = random.choice(first_names_irish_male)
            last_name = random.choice(last_names_irish)
        else:
            if gender == 'Female':
                first_name = random.choice(first_names_female)
            else:
                first_name = random.choice(first_names_male)
            last_name = random.choice(last_names)
        name = f"{first_name} {last_name}"
        # Assign operator to a random machine at their location
        location_machines = [m for m in machines_data if m[2] == location]
        assigned_machine = random.choice(location_machines)
        machine_id = assigned_machine[0]
        production_line = assigned_machine[3]
        shift = random.choice(shifts)
        operators_data.append((operator_id, name, gender, machine_id, production_line, location, shift))
        operator_id += 1

# Insert operators
cursor.executemany("""
INSERT INTO operators (operator_id, name, gender, machine_id, production_line, location, shift)
VALUES (?, ?, ?, ?, ?, ?, ?)
""", operators_data)

# Generate machine_metrics
operation_modes = ['Idle', 'Active']
start_time = datetime(2025, 11, 27, 8, 0, 0)
metrics_data = []
metric_id = 1

for machine in machines_data:
    machine_id = machine[0]
    location = machine[2]
    machine_operators = [op for op in operators_data if op[3] == machine_id]
    # If no operator assigned to this machine, pick one from the same location
    if not machine_operators:
        machine_operators = [op for op in operators_data if op[5] == location]
    for i in range(10):  # 10 metrics per machine
        timestamp = start_time + timedelta(minutes=10 * i)
        operation_mode = random.choice(operation_modes)
        operator = random.choice(machine_operators)
        operator_id = operator[0]
        gender = operator[2]
        # Machines operate better with female operators
        if gender == 'Female':
            temperature = round(random.uniform(30, 70), 2)
            vibration = round(random.uniform(0, 3), 5)
            power_consumption = round(random.uniform(1.5, 7), 5)
            defect_rate = round(random.uniform(0.2, 3), 5)
            units_produced = random.randint(80, 120)
        else:
            temperature = round(random.uniform(50, 84), 2)
            vibration = round(random.uniform(2, 5), 5)
            power_consumption = round(random.uniform(5, 10), 5)
            defect_rate = round(random.uniform(3, 10), 5)
            units_produced = random.randint(50, 90)
        planned_production_time = round(random.uniform(7.5, 10), 2)  # in hours
        metrics_data.append((
            metric_id,
            timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            machine_id,
            operator_id,
            operation_mode,
            temperature,
            vibration,
            power_consumption,
            defect_rate,
            units_produced,
            planned_production_time
        ))
        metric_id += 1

cursor.executemany("""
INSERT INTO machine_metrics (
    metric_id, timestamp, machine_id, operator_id, operation_mode, temperature, vibration, power_consumption, defect_rate, units_produced, planned_production_time
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", metrics_data)

conn.commit()
conn.close()

print("manufacturing_example_data.db file created.")