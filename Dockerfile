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

ENV PORT=4180 PYTHONUNBUFFERED=1
EXPOSE 4180
ENTRYPOINT ["docker-entrypoint.sh"]

# NOTE: -w 1 (single worker) is intentional. The app runs two background threads
# (_queue_keeper_loop, _broadcast_loop). With 2+ workers each thread would run 2x,
# producing duplicate AzuraCast /nextsong triggers and (without the broadcast-state
# lock) duplicate Telegram posts. If you need horizontal scaling, move the loops
# into a separate worker container with -w 1, keeping the web tier on >1 workers.
CMD ["gunicorn", "-w", "1", "-t", "120", "-b", "0.0.0.0:4180", "--access-logfile", "-", "app:app"]

