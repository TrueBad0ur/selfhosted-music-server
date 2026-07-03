FROM ghcr.io/neptunehub/audiomuse-ai:latest

# Fix: RQ dropped job.get_id() in favour of job.id
# https://github.com/NeptuneHub/AudioMuse-AI — unfiled upstream bug
RUN grep -rl 'job\.get_id()' /app/ | xargs sed -i 's/job\.get_id()/job.id/g'

# Install ROCm 6.4 runtime libraries for AMD GPU acceleration
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends wget gnupg ca-certificates && \
    mkdir -p --mode=0755 /usr/share/keyrings && \
    wget -qO - https://repo.radeon.com/rocm/rocm.gpg.key | gpg --dearmor -o /usr/share/keyrings/rocm.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/rocm.gpg] https://repo.radeon.com/rocm/apt/6.4/ noble main" \
        > /etc/apt/sources.list.d/rocm.list && \
    apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        rocblas hipblas hipsolver hipfft hipsparse miopen-hip rocsolver rocm-smi-lib && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Replace CPU onnxruntime with ROCm version
RUN pip uninstall -y onnxruntime && pip install onnxruntime-rocm==1.22.2.post1

# Patched task files:
#   clap_analyzer.py  — add ROCMExecutionProvider alongside CUDA
#   analysis.py       — add ROCMExecutionProvider alongside CUDA
#   collection_manager.py — treat PocketBase HTTPError as empty (PostgreSQL deployment)
COPY tasks/clap_analyzer.py /app/tasks/clap_analyzer.py
COPY tasks/analysis.py /app/tasks/analysis.py
COPY tasks/collection_manager.py /app/tasks/collection_manager.py
COPY tasks/clustering_helper.py /app/tasks/clustering_helper.py
