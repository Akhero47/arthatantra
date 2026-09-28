from python:3.12-slim

#prevent the creation of .pyc files
ENV PYTHONDONTWRITEBYTECODE=1
#prevent Python from buffering stdout and stderr
ENV PYTHONUNBUFFERED=1  

ENV PYTHONPATH=/app 

WORKDIR /app

# install python dependencies

copy requirements.txt .

run pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt   

# copy project files into the container
copy . .

# Run as non-root user
run useradd --create-home myuser \
    && chown -R myuser:myuser /app

USER myuser

expose 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]