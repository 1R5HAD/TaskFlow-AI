# Use a slim Python image to keep the container small
FROM python:3.11-slim

# Set working directory inside the container
WORKDIR /app

# Install dependencies first (Docker caches this layer — faster rebuilds)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app code
COPY . .

# Create a folder for the SQLite database file
# In production you'd mount a volume here so data persists across container restarts
RUN mkdir -p /app/instance

# Expose the port Flask runs on
EXPOSE 5000

# Run the app
# Using python directly so the DB is created on first startup via db.create_all()
CMD ["python", "app.py"]