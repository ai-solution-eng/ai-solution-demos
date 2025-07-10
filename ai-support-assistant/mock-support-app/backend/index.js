const express = require('express');
const cors = require('cors');
const { Pool } = require('pg');

const app = express();
const port = 3001;

app.use(cors());
app.use(express.json());

const pool = new Pool({
  user: 'demo',
  host: 'localhost',
  database: 'supportCases',
  password: 'demo123',
  port: 5432,
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

app.listen(port, () => {
  console.log(`Server running on http://localhost:${port}`);
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
