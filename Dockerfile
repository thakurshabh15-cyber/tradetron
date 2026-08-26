FROM python:3.11-slim
WORKDIR /app
COPY fastapi-template /app
RUN pip install fastapi uvicorn sqlalchemy pydantic pydantic-settings
EXPOSE 8000
CMD ["uvicorn", "app.webhooks.main:app", "--host", "0.0.0.0", "--port", "8000"]