FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Pristine copy of the default agents/ (personas + broadcasts.yaml). The live
# /app/agents is a Coolify persistent volume; the entrypoint seeds it from this
# copy on first run so a fresh volume isn't empty. See docker-entrypoint.sh.
COPY agents /app/agents_seed
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PORT=4180
EXPOSE 4180
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:4180", "--access-logfile", "-", "app:app"]
