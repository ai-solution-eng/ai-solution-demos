import random
import sqlite3
from datetime import datetime, timedelta

random.seed(42)

conn = sqlite3.connect("retail_example_data.db")
cursor = conn.cursor()
cursor.execute("PRAGMA foreign_keys = ON")

cursor.executescript("""
DROP TABLE IF EXISTS sale_items;
DROP TABLE IF EXISTS sales;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS stores;

CREATE TABLE stores (
    store_id INTEGER PRIMARY KEY,
    store_name TEXT NOT NULL,
    city TEXT NOT NULL,
    region TEXT NOT NULL
);

CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit_cost REAL NOT NULL,
    list_price REAL NOT NULL
);

CREATE TABLE sales (
    sale_id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    sale_timestamp DATETIME NOT NULL,
    payment_method TEXT NOT NULL,
    FOREIGN KEY (store_id) REFERENCES stores(store_id)
);

CREATE TABLE sale_items (
    sale_item_id INTEGER PRIMARY KEY,
    sale_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    discount_percent REAL NOT NULL,
    FOREIGN KEY (sale_id) REFERENCES sales(sale_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);
""")

stores_data = [
    (1, "Downtown Market", "Seattle", "West"),
    (2, "Lakeview Market", "Chicago", "Midwest"),
    (3, "Riverside Market", "Austin", "South"),
    (4, "Midtown Market", "Atlanta", "South"),
    (5, "Harbor Market", "Boston", "Northeast"),
]
products_data = [
    (1, "Ground Coffee", "Grocery", 6.00, 10.99),
    (2, "Green Tea", "Grocery", 3.50, 7.49),
    (3, "Olive Oil", "Grocery", 7.00, 12.99),
    (4, "Pasta", "Grocery", 1.20, 2.99),
    (5, "Dark Chocolate", "Grocery", 2.00, 4.49),
    (6, "Trail Mix", "Grocery", 3.25, 6.99),
    (7, "Dish Soap", "Household", 1.50, 3.49),
    (8, "Laundry Detergent", "Household", 5.00, 9.99),
    (9, "Paper Towels", "Household", 2.75, 5.49),
    (10, "Storage Bins", "Household", 4.50, 8.99),
    (11, "Water Bottle", "Outdoors", 5.00, 12.99),
    (12, "Camping Lantern", "Outdoors", 12.00, 24.99),
    (13, "Travel Mug", "Outdoors", 6.00, 14.49),
    (14, "Yoga Mat", "Fitness", 10.00, 22.99),
    (15, "Resistance Bands", "Fitness", 4.00, 9.99),
    (16, "Running Socks", "Apparel", 3.00, 7.99),
    (17, "Cotton T-Shirt", "Apparel", 7.00, 16.99),
    (18, "Baseball Cap", "Apparel", 6.00, 14.99),
    (19, "Phone Charger", "Electronics", 8.00, 17.99),
    (20, "Wireless Earbuds", "Electronics", 24.00, 49.99),
]

cursor.executemany(
    "INSERT INTO stores VALUES (?, ?, ?, ?)", stores_data
)
cursor.executemany(
    "INSERT INTO products VALUES (?, ?, ?, ?, ?)", products_data
)

sales_data = []
sale_items_data = []
sale_id = 1
sale_item_id = 1
start_date = datetime(2025, 1, 1)
payment_methods = ["Cash", "Credit Card", "Debit Card", "Mobile Wallet"]

for day in range(30):
    sale_date = start_date + timedelta(days=day)
    for store_id, _, _, _ in stores_data:
        for _ in range(15):
            sale_time = sale_date.replace(
                hour=random.randint(9, 20),
                minute=random.randint(0, 59),
                second=random.randint(0, 59),
            )
            sales_data.append((
                sale_id,
                store_id,
                sale_time.strftime("%Y-%m-%d %H:%M:%S"),
                random.choice(payment_methods),
            ))

            for product in random.sample(products_data, random.randint(1, 4)):
                product_id = product[0]
                list_price = product[4]
                discount_percent = random.choice([0, 0, 0, 5, 10, 15])
                unit_price = round(list_price * (1 - discount_percent / 100), 2)
                sale_items_data.append((
                    sale_item_id,
                    sale_id,
                    product_id,
                    random.randint(1, 4),
                    unit_price,
                    discount_percent,
                ))
                sale_item_id += 1
            sale_id += 1

cursor.executemany(
    "INSERT INTO sales VALUES (?, ?, ?, ?)", sales_data
)
cursor.executemany(
    "INSERT INTO sale_items VALUES (?, ?, ?, ?, ?, ?)", sale_items_data
)

conn.commit()
conn.close()

print("retail_example_data.db file created with retail sales data.")
