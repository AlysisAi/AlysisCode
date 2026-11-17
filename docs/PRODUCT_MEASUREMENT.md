# Optional CLI active-day measurement

Off by default. Set `ALYSIS_USAGE_METRICS=1` to contribute anonymous CLI active-day counts.
Set `DO_NOT_TRACK=1` or remove the opt-in to stop. No login is required.

A submitted `run` task or interactive chat task sends one best-effort event per UTC day,
with a random persistent measurement ID, CLI version, OS and explicit `cli` kind.
No prompt, path, account, hostname, model response or credentials are sent. Login/status
commands and idle terminals are excluded. Desktop-hosted engines suppress CLI events;
the desktop app has its own independent preference and foreground measurement.

The dashboard counts active installations, not unique people. Existing CLI profiles are
reported as an existing-install baseline, never reconstructed as new installations.
Daily records are retained for 90 days; inactive installation records expire after 395 days.
Metrics start only after an updated client ships and the person opts in.

Public routing configuration is discovered over HTTPS from the configured first-party
site only after opt-in. No deployment key is bundled in the CLI. Discovery is bounded
to 4 KB, refuses redirects and accepts only an anonymous routing key for the expected API.
Custom deployments use their existing `ALYSIS_CLOUD_API_URL` and `ALYSIS_CLOUD_PUBLIC_KEY`.
