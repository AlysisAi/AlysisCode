# Portable worker filesystem. Build the qualified Linux runtime first, then
# supply its explicit local/registry reference. No Azure SDK or credential.
ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}
COPY --chown=1000:1000 src /opt/alysis/src
ENV PYTHONPATH=/opt/alysis/src
USER 1000:1000
WORKDIR /var/lib/alysis/workspace
# No VNC listener, API or desktop is started before task authority is supplied.
ENTRYPOINT []
CMD ["sleep", "infinity"]
