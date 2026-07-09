const tasks = require("./routes/tasks");
const connection = require("./db");
const cors = require("cors");
const express = require("express");
const app = express();

connection();

app.use(express.json());
app.use(cors());

const mongoose = require("mongoose");

app.get('/ok', async (req, res) => {
    try {
        if (mongoose.connection.readyState !== 1) {
            return res.status(500).send('database disconnected');
        }
        await mongoose.connection.db.admin().ping();
        res.status(200).send('ok');
    } catch (err) {
        res.status(500).send('database ping failed');
    }
});

app.use("/api/tasks", tasks);

const port = process.env.PORT || 3500;
app.listen(port, () => console.log(`Listening on port ${port}...`));
