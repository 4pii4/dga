FROM python:3.14-alpine AS builder

COPY requirements.txt .
RUN pip --no-cache-dir install -r ./requirements.txt

FROM python:3.14-alpine

WORKDIR /dga
RUN addgroup -g 1000 -S dga && adduser -u 1000 -S dga -G dga && chown -R dga:dga /dga && chmod -R 700 /dga
ENV MAGICK_HOME=/usr
RUN apk add --no-cache imagemagick-dev ffmpeg
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --chown=dga:dga dga.py .
USER dga
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python3", "/dga/dga.py", "--config", "/dga/config.json"]