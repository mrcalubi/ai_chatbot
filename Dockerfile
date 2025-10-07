# Use a small official Python image
FROM python:3.11-slim

WORKDIR /app

# Copy dependency list and install it first (good for caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the rest of your code into the image
COPY . /app

# Open port 8005 (where your app will listen)
EXPOSE 8005

# Start your FastAPI app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8005"]
