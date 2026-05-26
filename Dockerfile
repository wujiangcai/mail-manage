FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV MAIL_CODE_HOST=0.0.0.0
ENV MAIL_CODE_PORT=17373

WORKDIR /app

COPY app.py /app/app.py
COPY static /app/static

RUN useradd --create-home --shell /usr/sbin/nologin appuser
USER appuser

EXPOSE 17373

CMD ["python", "app.py"]
