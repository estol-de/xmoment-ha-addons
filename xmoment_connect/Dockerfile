ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN pip install --no-cache-dir aiohttp==3.10.11 zeroconf==0.132.2

COPY app /app

# s6-overlay startet CMD ohne Container-Umgebung -> with-contenv holt SUPERVISOR_TOKEN zurueck
CMD ["/command/with-contenv", "python3", "-u", "/app/app.py"]
