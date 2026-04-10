FROM python:3.14-alpine AS builder
WORKDIR /dga
COPY requirements.txt .
RUN pip3.14 install -r ./requirements.txt

FROM python:3.14-alpine
WORKDIR /dga
COPY . .
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
RUN addgroup -g 1000 -S dga && adduser -u 1000 -S dga -G dga
RUN chown dga:dga /dga
RUN apk add --no-cache ffmpeg imagemagick
USER dga
CMD ["python3.14", "/dga/dga.py", "--config", "/dga/config.json"]
