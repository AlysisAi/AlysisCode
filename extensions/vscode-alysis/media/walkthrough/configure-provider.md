# Configure a provider

Run **Configure Provider** to choose your provider and model. Hosted providers require an API key.
For a local endpoint such as Ollama or LM Studio, leave the optional key blank unless your server
requires authentication. Any key you supply is stored in VS Code SecretStorage. The key
is passed to the CLI through the child process environment — never written to settings, command
lines, or logs.

The extension redacts secrets from any error or diagnostic output it shows.
