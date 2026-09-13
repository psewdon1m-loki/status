FROM ghcr.io/xtls/xray-core:26.9.9@sha256:45338c4df61fda061c47ce62aafda6c5d7d59cbdefc33f2e335d8b0c748b748a AS xray

FROM python:3.12-alpine@sha256:d09d15e60962ca365d1cd544a48773bac9d33f2fb1b00f2aa0deec78ade7dc31

ARG STATUS_VERSION=0.0.1
LABEL org.opencontainers.image.title="Cake Status" \
      org.opencontainers.image.version="${STATUS_VERSION}"

WORKDIR /app

RUN addgroup -S status \
    && adduser -S -D -H -G status status \
    && mkdir -p /data \
    && chown status:status /data

COPY service /app/service
COPY static /app/static
COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray

USER status

EXPOSE 8080

CMD ["python", "/app/service/app.py"]
