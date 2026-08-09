# Security Policy

Writing Canvas is a local, single-user tool.

## Supported boundary

The service is intended to bind only to `127.0.0.1`. It has no remote
multi-user authentication and must not be exposed through a public interface,
reverse proxy, port forwarding, or a listener bound to `0.0.0.0`.

The runtime data directory may contain private documents and review content.
Do not include it in bug reports, screenshots, commits, or public archives.

## Reporting

Do not publish sensitive security details in a public issue. Contact the
repository maintainer privately through the GitHub profile associated with this
repository and include a minimal reproduction without secrets or private data.
