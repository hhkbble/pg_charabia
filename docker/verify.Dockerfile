FROM rust:1.97.1-bookworm@sha256:0e2bcaef56d041a486784e54104a81aebe0da44bd03019bd70bc0401e42e4a97 AS rust
FROM postgres:18.6-bookworm@sha256:afc7e2d441324c0388fa80c3d24f733b4194a4eb7f47dd8ee2b08eb1a24a647c
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
ENV RUSTUP_HOME=/usr/local/rustup
ENV PATH=/usr/local/cargo/bin:/usr/lib/postgresql/18/bin:$PATH
ENV LIBCLANG_PATH=/usr/lib/llvm-19/lib
ENV CARGO_BUILD_JOBS=2
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential postgresql-server-dev-18 clang-19 libclang-19-dev llvm-19-dev \
    pkg-config libssl-dev ca-certificates curl git file binutils python3 procps \
    && dpkg-query -W > /opt/pgcb-packages.txt \
    && rm -rf /var/lib/apt/lists/*
RUN rustc -Vv && rustup component add clippy rustfmt \
    && cargo install cargo-pgrx --version 0.19.3 --locked
RUN install -m 755 /root/.cargo/bin/cargo-pgrx /usr/local/cargo/bin/cargo-pgrx
ARG PG_TEXTSEARCH_COMMIT=fa142d049d08eac45a263f5abc250c8877ae076e
RUN mkdir /tmp/pg_textsearch \
    && curl -fL --retry 0 https://github.com/timescale/pg_textsearch/archive/${PG_TEXTSEARCH_COMMIT}.tar.gz -o /tmp/pg_textsearch.tar.gz \
    && printf '445a546810cbc17e15d05ff8a391f1b693eab382a2ef6efbf72e1c37fa0c48d0  /tmp/pg_textsearch.tar.gz\n' | sha256sum --check \
    && sha256sum /tmp/pg_textsearch.tar.gz > /opt/pgcb-pg-textsearch-source.sha256 \
    && tar -xzf /tmp/pg_textsearch.tar.gz -C /tmp/pg_textsearch --strip-components=1 \
    && make -C /tmp/pg_textsearch -j2 PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config \
    && make -C /tmp/pg_textsearch install PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config \
    && cp /tmp/pg_textsearch/LICENSE /opt/pgcb-pg-textsearch-LICENSE \
    && rm -rf /tmp/pg_textsearch /tmp/pg_textsearch.tar.gz
RUN mkdir -p /cache/cargo /cache/target /cache/lindera /cache/resources /evidence \
    && chown -R postgres:postgres /cache /evidence \
    && chmod -R a+rX /usr/local/cargo /usr/local/rustup
ENV CARGO_HOME=/cache/cargo
ENV PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config
ENV PGCB_TARGET_BASE=/cache/target
ENV LINDERA_BUILD_DICTIONARY_CACHE_DIR=/cache/lindera
ENV PGCB_ARTIFACTS=/evidence
ENV PGCB_RESOURCE_ARCHIVES=/cache/resources
WORKDIR /workspace
USER postgres
ENTRYPOINT ["python3", "/workspace/scripts/run-environment.py", "--platform", "linux-amd64"]
