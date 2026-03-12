FROM ghcr.io/neptunehub/audiomuse-ai:latest

# Fix: RQ dropped job.get_id() in favour of job.id
# https://github.com/NeptuneHub/AudioMuse-AI — unfiled upstream bug
RUN grep -rl 'job\.get_id()' /app/ | xargs sed -i 's/job\.get_id()/job.id/g'
