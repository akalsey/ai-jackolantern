FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=3000
WORKDIR /app

# Dependencies come from the script's inline metadata, so they're declared once.
# Changing them needs a rebuild: docker compose up -d --build.
COPY pumpkin_agent.py child_model.py costume_log.py live_log.py qc_monitor.py reload_filter.py remote_control.py reset_training.py returning_visitors.py signalwire_setup.py visit_pacing.py visit_state.py ./
RUN uv export --script pumpkin_agent.py --no-hashes > /tmp/requirements.txt \
 && uv pip install --system --no-cache -r /tmp/requirements.txt watchfiles==1.3.0 \
 && rm /tmp/requirements.txt

# A copy of the code for running the image on its own. In production,
# docker-compose mounts the live checkout over /app.
COPY eyes/ eyes/
# The call-logs volume takes this directory's owner when Docker first creates it.
RUN useradd --system pumpkin && mkdir -p /data/call-logs && chown pumpkin /data/call-logs
USER pumpkin

EXPOSE 3000
# Restarts the agent process (not the container) when Python code or
# surroundings.txt changes (reload_filter.py). Files in eyes/ are read on each
# request, so they need no restart.
CMD ["watchfiles", "--filter", "reload_filter.CodeAndSurroundings", "python pumpkin_agent.py", "/app"]
