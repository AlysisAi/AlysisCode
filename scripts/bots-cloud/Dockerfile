# Portable worker filesystem. Build the qualified Linux runtime first, then
# supply its explicit local/registry reference. No Azure SDK or credential.
ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}
USER root
WORKDIR /opt/alysis
COPY scripts/bots-cloud/install-chrome.sh /opt/alysis/install-chrome.sh
RUN sh /opt/alysis/install-chrome.sh && rm -rf /var/lib/apt/lists/*
RUN apt-get update && apt-get install -y --no-install-recommends x11vnc && rm -rf /var/lib/apt/lists/*
RUN test -x /usr/bin/tint2 && test -x /usr/bin/hsetroot && test -x /usr/bin/xdotool && test -x /usr/bin/pcmanfm && test -x /usr/bin/xterm
COPY pyproject.toml uv.lock README.md hatch_build.py ./
# A reused runtime may contain modules removed from this revision.
RUN rm -rf /opt/alysis/src
COPY --chown=1000:1000 src /opt/alysis/src
# Isolated subprocesses import the installed package, not PYTHONPATH. Keep it
# and its locked dependencies at the same revision as the worker source.
RUN uv sync --locked --no-dev --no-editable --extra server --extra documents && uv cache clean
# pip for the owl's own package installs (package_access.py): uv environments ship without it.
# Python's bundled copy, so the image needs no download for it; owls install into their computer.
RUN /opt/alysis/.venv/bin/python -m ensurepip --default-pip && /opt/alysis/.venv/bin/python -m pip --version
COPY scripts/bots-cloud/check_image.py /opt/alysis/check_image.py
ENV PYTHONPATH=/opt/alysis/src
USER 1000:1000
WORKDIR /var/lib/alysis/workspace
# No VNC listener, API or desktop is started before task authority is supplied.
ENTRYPOINT []
CMD ["sleep", "infinity"]
