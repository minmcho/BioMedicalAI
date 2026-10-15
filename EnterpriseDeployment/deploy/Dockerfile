FROM python:3.12-slim
WORKDIR /srv/app
COPY app/ ./app/
RUN useradd -r appuser && chown -R appuser /srv/app
USER appuser
# Placeholder service entrypoint: replaced by the API service in Week 3 of the plan.
CMD ["python", "-m", "app.tenant_store"]
