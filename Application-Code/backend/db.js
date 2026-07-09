const mongoose = require("mongoose");

const connectWithRetry = async () => {
    const connectionParams = {
        useNewUrlParser: true,
        useUnifiedTopology: true,
        maxPoolSize: 10,                 // Connection pool limit
        serverSelectionTimeoutMS: 5000,  // Fast fail timeout for retries
        socketTimeoutMS: 45000,          // Close inactive sockets
    };

    const useDBAuth = process.env.USE_DB_AUTH || false;
    if (useDBAuth) {
        connectionParams.user = process.env.MONGO_USERNAME;
        connectionParams.pass = process.env.MONGO_PASSWORD;
    }

    console.log("Attempting MongoDB connection...");

    try {
        await mongoose.connect(process.env.MONGO_CONN_STR, connectionParams);
    } catch (error) {
        console.error("MongoDB connection failed. Retrying in 5 seconds...", error.message);
        setTimeout(connectWithRetry, 5000); // Retry loop
    }
};

// Monitor connection lifecycle events
mongoose.connection.on("connected", () => {
    console.log("Mongoose connected successfully to MongoDB.");
});

mongoose.connection.on("error", (err) => {
    console.error(`Mongoose connection error: ${err}`);
});

mongoose.connection.on("disconnected", () => {
    console.warn("Mongoose connection lost. MongoDB disconnected.");
});

module.exports = connectWithRetry;
