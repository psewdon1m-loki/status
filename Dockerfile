FROM python:3.12-alpine@sha256:d09d15e60962ca365d1cd544a48773bac9d33f2fb1b00f2aa0deec78ade7dc31

WORKDIR /app

RUN apk upgrade --no-cache \
    && addgroup -S status \
    && adduser -S -D -H -G status status \
    && mkdir -p /data \
    && chown status:status /data

COPY service /app/service
COPY static /app/static

USER status

EXPOSE 8080

CMD ["python", "/app/service/app.py"]
