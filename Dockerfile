FROM alpine:3.20

RUN apk add --no-cache python3 py3-pip nginx curl unzip bash tzdata ca-certificates

WORKDIR /app
COPY requirements.txt .

# Build deps are only needed to compile Pillow/psutil wheels; drop them after.
RUN apk add --no-cache --virtual .build-deps \
        gcc musl-dev python3-dev zlib-dev jpeg-dev freetype-dev linux-headers \
 && pip3 install --no-cache-dir --break-system-packages -r requirements.txt \
 && apk del .build-deps

# Xray-core. The archive also carries geoip.dat/geosite.dat, which routing
# rules can use; the private-range rules work without them regardless.
RUN curl -fsSL -o /tmp/xray.zip \
        "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip" \
 && unzip -o /tmp/xray.zip -d /usr/local/bin/ \
 && chmod +x /usr/local/bin/xray \
 && rm /tmp/xray.zip

COPY . /app
COPY nginx.conf /etc/nginx/nginx.conf
RUN chmod +x /app/entrypoint.sh

# The panel binds this; nginx owns whatever public port the platform assigns.
ENV PANEL_PORT=10000
EXPOSE 8000

CMD ["/app/entrypoint.sh"]
