import express from "express";
import path from "path";
import { fileURLToPath } from "url";
import history from "connect-history-api-fallback";
// const { Pool } = require('pg');

import { Pool } from "pg";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();
const port = process.env.MOCK_SUPPORT_APP_PORT || 3001;
const host = '0.0.0.0'

const poolConfig = {
  user: process.env.MOCK_SUPPORT_APP_DBUSER || "demo",
  host: process.env.MOCK_SUPPORT_APP_DBHOST || "host.docker.internal",
  database: process.env.MOCK_SUPPORT_APP_DBNAME || "supportCases",
  password: process.env.MOCK_SUPPORT_APP_DBPASSWORD || "demo123",
  port: process.env.MOCK_SUPPORT_APP_DBPORT || 5432
};

const pool = new Pool(poolConfig);

// Print config
console.log(`Database host: ${poolConfig.host}`);
console.log(`Database port: ${poolConfig.port}`);
console.log(`Database name: ${poolConfig.database}`);
console.log(`Database user: ${poolConfig.user}`);
console.log(`Database password: ${poolConfig.password}`);

// Middleware
app.use(express.json());

// --- API ROUTES ---

// Health check
app.get('/api/health', (req, res) => {
  res.sendStatus(200);
});

// Get all items
app.get('/api/cases', async (req, res) => {
  const { rows } = await pool.query('SELECT id, subject FROM cases');
  res.json(rows);
});

// Get details for an item
app.get('/api/cases/:id/details', async (req, res) => {
  const { id } = req.params;
  const { rows } = await pool.query(
    'SELECT id, case_id, msg, msg_type FROM msgs WHERE case_id = $1 ORDER BY id DESC',
    [id]
  );
  res.json(rows);
});

// Create a new item and associated detail
app.post('/api/cases', async (req, res) => {
  const { subject, msg } = req.body;

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    const insertItem = await client.query(
      'INSERT INTO cases (subject) VALUES ($1) RETURNING id',
      [subject]
    );
    const itemId = insertItem.rows[0].id;

    await client.query(
      'INSERT INTO msgs (case_id, msg, msg_type) VALUES ($1, $2, \'Message from Customer\')',
      [itemId, msg]
    );

    await client.query('COMMIT');
    res.status(201).json({ success: true, itemId });
  } catch (err) {
    await client.query('ROLLBACK');
    res.status(501).json({ success: false, error: err.message });
  } finally {
    client.release();
  }

});

// --- History fallback for SPA ---
app.use(history());

// --- Serve frontend ---
app.use(express.static(path.join(__dirname, "../frontend/dist")));

// Start the server listening
app.listen(port, host, () => {
  console.log(`Server running on http://${host}:${port}`);
});