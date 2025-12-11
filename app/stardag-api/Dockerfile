FROM python:3.11-slim

WORKDIR /app

# Install uv
RUN pip install uv

# Copy everything needed for install
COPY pyproject.toml .
COPY src/ src/

# Install the package
RUN uv pip install --system .

EXPOSE 8000

CMD ["uvicorn", "stardag_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
