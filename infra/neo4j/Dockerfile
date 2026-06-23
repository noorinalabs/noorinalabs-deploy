# Custom Neo4j image with GDS plugin pre-installed.
# The GDS plugin requires downloading from graphdatascience.ninja at startup,
# which fails when the container is on an internal-only Docker network.
# Pre-installing the JAR into /var/lib/neo4j/products/ lets the entrypoint
# detect it locally and skip the download.
#
# When upgrading Neo4j, check the compatible GDS version at:
#   https://graphdatascience.ninja/versions.json

ARG NEO4J_VERSION=5-community
# The official `neo4j` vendor image is consumed as-is, with the version chosen by
# the NEO4J_VERSION build-arg so Neo4j<->GDS compatibility stays adjustable at
# build time (see the GDS-version note above). A `@sha256:` digest pin would
# hard-code the version and defeat that parameterization, and we add no
# app-controlled package layer here (only the GDS jar below).
# RATIONALE: build-arg-parameterized vendor base (tech-decisions.md § Base Image Pinning, noorinalabs-main#735); digest-pin + apt-upgrade intentionally exempted for this single FROM.
FROM neo4j:${NEO4J_VERSION}

ARG GDS_VERSION=2.13.8
ARG GDS_JAR_URL=https://graphdatascience.ninja/neo4j-graph-data-science-${GDS_VERSION}.jar

RUN wget -q --timeout=120 -O \
    /var/lib/neo4j/products/neo4j-graph-data-science-${GDS_VERSION}.jar \
    "${GDS_JAR_URL}"
