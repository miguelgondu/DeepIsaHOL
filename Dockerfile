# DeepIsaHOL Dockerfile
# Multi-stage build for containerizing Isabelle/HOL proof verification API

# =============================================================================
# Stage 1: Builder
# =============================================================================
# Force amd64 platform - Isabelle2025-2_linux.tar.gz contains x86_64 binaries
FROM --platform=linux/amd64 ubuntu:22.04 AS builder

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install build dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    openjdk-17-jdk \
    python3 \
    python3-pip \
    gnupg \
    apt-transport-https \
    && rm -rf /var/lib/apt/lists/*

# Install sbt
RUN echo "deb https://repo.scala-sbt.org/scalasbt/debian all main" | tee /etc/apt/sources.list.d/sbt.list && \
    echo "deb https://repo.scala-sbt.org/scalasbt/debian /" | tee /etc/apt/sources.list.d/sbt_old.list && \
    curl -sL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x2EE0EA64E40A89B84B2DF73499E82A75642AC823" | apt-key add && \
    apt-get update && \
    apt-get install -y sbt && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Download and extract Isabelle
# SHA256 from https://isabelle.in.tum.de/dist/
# NOTE: the plain /dist/ URL 302s to dist.isabelle.cit.tum.de, a mirror that is
# unreachable from some networks (connection just times out). The /website-.../dist/
# path below serves the same file directly from isabelle.in.tum.de with no redirect.
RUN echo "Downloading Isabelle 2025-2..." && \
    curl -sLO https://isabelle.in.tum.de/website-Isabelle2025-2/dist/Isabelle2025-2_linux.tar.gz && \
    echo "a20a507bc7c1270d8be96a9f3fbec06345387789d2dc2c4d3df6260d47bfb33c  Isabelle2025-2_linux.tar.gz" | sha256sum -c - && \
    tar -xzf Isabelle2025-2_linux.tar.gz && \
    rm Isabelle2025-2_linux.tar.gz

# NOTE: the Archive of Formal Proofs (AFP) is intentionally NOT installed in this
# image. The /verify path runs against the prebuilt HOL heap and never resolves AFP
# sessions (see Utils.logics_map / Isa_Minion), and nothing that consumes this API
# submits AFP-dependent theories or ROOTs. Bundling it added several GB to the image
# plus a fragile rolling-snapshot checksum pin. To re-add it, restore the download +
# `isabelle components -u ./afp/thys/` here and the matching COPY + register in the
# runtime stage, and point Directories.isabelle_afp at /app/afp/thys/ below.

# Which Isabelle library sessions get prebuilt into the `Benchmark` heap.
# Defaults to the cheapest possible heap (bare HOL, which ships prebuilt with
# the distribution, so the isabelle build below is a no-op). Set these to
# reproduce the old always-on Analysis/Probability/+8-siblings heap, e.g.:
#   --build-arg DEEPISAHOL_PARENT_SESSION=HOL-Probability
#   --build-arg DEEPISAHOL_EXTRA_SESSIONS=HOL-Number_Theory,HOL-Algebra,HOL-Combinatorics,HOL-Cardinals,HOL-Computational_Algebra,HOL-Decision_Procs,HOL-Real_Asymp,HOL-Eisbach,HOL-Library
# See DeepIsaHOL/README.md for the full list of supported session names and
# what each one buys you; benchmark/gen_root.py errors out (naming the
# supported list) on anything it doesn't recognize.
ARG DEEPISAHOL_PARENT_SESSION="HOL"
ARG DEEPISAHOL_EXTRA_SESSIONS=""

# Pre-build the Isabelle library session heaps requested above. `-o system_heaps`
# writes the heaps into the distribution tree ($ISABELLE_HOME/heaps) instead of
# the per-user dir, so the existing `COPY --from=builder /build/Isabelle2025-2`
# in the runtime stage carries them (the runtime `isabelle build` still searches
# the system heap dir as a fallback with system_heaps=false). Building
# `Benchmark` also builds+persists every session it depends on.
#
# With the defaults above this layer is trivial (seconds). With the full
# Analysis/Probability/+8-siblings config it's EXPENSIVE: ~1-2 h wall time.
# It caches until the generated benchmark/ROOT changes (i.e. until either
# DEEPISAHOL_* arg above changes, or benchmark/gen_root.py itself does).
#
# Parallelism (override at build time, e.g. --build-arg BENCHMARK_THREADS=6)
# only matters for non-trivial configs:
#   BENCHMARK_THREADS - `-o threads`, parallelism *within* a session. Empty (the
#                       default) => auto: all CPUs the build sees ($(nproc)),
#                       capped at ~1 per 3 GB of RAM so it can't OOM-kill itself
#                       (each Isabelle worker wants 2-4 GB). This is the knob that
#                       speeds up the big serial sessions (HOL-Analysis/Probability).
#   BENCHMARK_JOBS    - `-j`, how many sessions build concurrently (default 1).
#                       Helps the sibling-session tail; total load is roughly
#                       JOBS * THREADS, so keep that under your core/RAM budget.
ARG BENCHMARK_THREADS=""
ARG BENCHMARK_JOBS=1
COPY benchmark /build/benchmark
RUN DEEPISAHOL_PARENT_SESSION="${DEEPISAHOL_PARENT_SESSION}" \
    DEEPISAHOL_EXTRA_SESSIONS="${DEEPISAHOL_EXTRA_SESSIONS}" \
    python3 /build/benchmark/gen_root.py > /build/benchmark/ROOT && \
    echo "Generated benchmark/ROOT:" && cat /build/benchmark/ROOT
RUN set -eu; \
    cores="$(nproc)"; \
    mem_gb="$(awk '/^MemTotal:/ {printf "%d", $2 / 1024 / 1024}' /proc/meminfo)"; \
    [ -n "$mem_gb" ] && [ "$mem_gb" -ge 1 ] 2>/dev/null || mem_gb=4; \
    mem_cap=$(( mem_gb / 3 )); \
    if [ "$mem_cap" -lt 1 ]; then mem_cap=1; fi; \
    auto="$cores"; \
    if [ "$auto" -gt "$mem_cap" ]; then auto="$mem_cap"; fi; \
    threads="${BENCHMARK_THREADS:-$auto}"; \
    echo "Pre-building benchmark heaps: cores=$cores mem=${mem_gb}G -> threads=$threads jobs=${BENCHMARK_JOBS}"; \
    echo "  (override with --build-arg BENCHMARK_THREADS=N / BENCHMARK_JOBS=N)"; \
    ./Isabelle2025-2/bin/isabelle build -b -o system_heaps \
        -j"${BENCHMARK_JOBS}" -o threads="$threads" -D /build/benchmark

# Clone and build scala-isabelle dependency
RUN git clone https://github.com/dominique-unruh/scala-isabelle.git && \
    cd scala-isabelle && \
    sbt publishLocal

# Copy project files
COPY build.sbt /build/app/
COPY src /build/app/src/

WORKDIR /build/app

# Generate directories.scala with container paths
RUN cat > /build/app/src/main/scala/directories.scala << 'EOF'
/*
Mantainers:
Jonathan Julián Huerta y Munive huertjon[at]cvut[dot]cz

Isabelle/RL directories: Docker container configuration
NOTE: This file is auto-generated by the Dockerfile.
Do not edit manually - changes will be overwritten during Docker builds.
*/

package isabelle_rl

object Directories {
val isabelle_app = "/app/Isabelle2025-2/"
val isabelle_afp = "/app/afp/thys/" // AFP not installed in this image; inert path (Utils.valid_afp -> false)
val isabelle_rl = "/app/"
}
EOF

# Compile the project with sbt
RUN sbt compile

# =============================================================================
# Stage 2: Runtime
# =============================================================================
# Force amd64 platform to match builder stage
FROM --platform=linux/amd64 ubuntu:22.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    openjdk-17-jre-headless \
    python3 \
    python3-pip \
    polyml \
    libgomp1 \
    gnupg \
    apt-transport-https \
    curl \
    dos2unix \
    && rm -rf /var/lib/apt/lists/*

# Install sbt (needed to run the gateway)
RUN echo "deb https://repo.scala-sbt.org/scalasbt/debian all main" | tee /etc/apt/sources.list.d/sbt.list && \
    echo "deb https://repo.scala-sbt.org/scalasbt/debian /" | tee /etc/apt/sources.list.d/sbt_old.list && \
    curl -sL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x2EE0EA64E40A89B84B2DF73499E82A75642AC823" | apt-key add && \
    apt-get update && \
    apt-get install -y sbt && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy Isabelle from builder
COPY --from=builder /build/Isabelle2025-2 /app/Isabelle2025-2

# Copy compiled project from builder
COPY --from=builder /build/app /app

# Copy the benchmark session dir and register it globally, so `isabelle build`
# (used by the /build endpoint) can resolve `Benchmark` and the library sessions
# as session parents. The prebuilt heaps themselves rode in with the Isabelle tree.
COPY --from=builder /build/benchmark /app/benchmark
RUN printf '%s\n' '/app/benchmark' >> /app/Isabelle2025-2/ROOTS

# Copy sbt cache for faster startup
COPY --from=builder /root/.sbt /root/.sbt
COPY --from=builder /root/.cache /root/.cache

# Copy scala-isabelle local publish
COPY --from=builder /root/.ivy2/local /root/.ivy2/local

# Copy docker-specific files
COPY docker/requirements.txt /app/docker/
COPY docker/api.py /app/docker/
COPY docker/entrypoint.sh /app/docker/

# Intento solución para jacobo (compatibilidad windows)
# Usamos dos2unix para limpiar el script y luego damos permisos
RUN dos2unix /app/docker/entrypoint.sh && chmod +x /app/docker/entrypoint.sh

# Install Python dependencies
RUN pip3 install --no-cache-dir -r /app/docker/requirements.txt

# Make entrypoint executable
RUN chmod +x /app/docker/entrypoint.sh

# Expose the API port
EXPOSE 8000

# Set environment variables
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH="${JAVA_HOME}/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run the entrypoint script
ENTRYPOINT ["/app/docker/entrypoint.sh"]
