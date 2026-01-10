const express = require('express');
const sqlite3 = require('sqlite3').verbose();
const cors = require('cors');
const app = express();

app.use(cors());
app.use(express.json());

const db = new sqlite3.Database('/app/data/jobhunt.db');

db.serialize(() => {
    db.run(`CREATE TABLE IF NOT EXISTS jobs (
        ID TEXT PRIMARY KEY, 
        Company TEXT, 
        Role TEXT, 
        Status TEXT,
        Stage TEXT,
        LastEmail TEXT
    )`);
});

app.get('/api/jobs', (req, res) => {
    db.all("SELECT * FROM jobs", [], (err, rows) => {
        res.json(rows);
    });
});

app.post('/api/update-status', (req, res) => {
    const { id, status } = req.body;
    db.run("UPDATE jobs SET Status = ?, Stage = ? WHERE ID = ?", [status, status, id], (err) => {
        res.json({ success: true });
    });
});

app.listen(5000, () => console.log('SQLite API running on port 5000'));