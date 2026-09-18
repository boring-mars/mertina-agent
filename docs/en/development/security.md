# Security

**English** | [中文](../../zh/development/security.md)

Mertina Agent runs in the cloud, handles user credentials and runs LLM agents that can call tools.
Security mistakes affect real users, so these rules are not optional.

## Reporting a vulnerability

**Do not open a public issue, PR or Discussion for a security vulnerability.**

- Report it privately through GitHub: go to the repository's **Security** tab and choose **Report a vulnerability**
  (private vulnerability reporting)
- Include a description, affected versions, steps to reproduce and the potential impact
- The maintainer will acknowledge the report within **3 working days** and keep you informed about the fix
- The fix is developed in a private security advisory, released, and then the advisory is published.
  Reporters are credited unless they prefer not to be
- Please give us a reasonable time to fix the problem (normally up to 90 days) before disclosing it publicly

Only the latest release receives security fixes.

## Secrets

- **Never commit secrets**: API keys, tokens, passwords, private keys, cloud credentials, connection strings with passwords
- Configuration is read from environment variables. `.env` is in `.gitignore`. Only `.env.example` with placeholder values is committed
- Tests and examples use obviously fake values (`sk-test-000`), never real ones
- GitHub **secret scanning** and **push protection** are enabled. If a push is blocked, remove the secret. Don't bypass the check
- **If a secret is committed, even on a branch, even for a minute, treat it as leaked:**
  1. Revoke or rotate it immediately at the provider. This is the only step that actually fixes the problem
  2. Tell the maintainer
  3. Removing it from Git history is secondary. It has probably already been copied
- CI secrets are stored as GitHub Actions secrets, scoped as narrowly as possible. Workflows triggered by PRs from forks
  have no access to secrets

## Secure coding rules

**Input and injection**
- Validate all external input at the boundary: type, length, format, allowed values
- Use parameterized queries. Never build SQL by string concatenation
- Never pass user input to a shell. If you must run a subprocess, use an argument list, not `shell=True`
- Resolve and check file paths so user input can't escape the intended directory
- Don't deserialize untrusted data with `pickle`, `yaml.load` (use `yaml.safe_load`) or `eval`

**Authentication, authorization and tenants**
- Every endpoint requires authentication unless it is explicitly public (for example the health check)
- Check authorization on every request, on the server. Don't rely on the client
- Every query that touches user data is scoped to the current tenant or user. Test that one tenant can't reach another's data
- Compare secrets with `hmac.compare_digest`, not `==`

**LLM agents and tools**
- Treat model output as untrusted input. Validate tool arguments before executing them
- Content retrieved by the agent (web pages, files, tool results) may contain prompt injection. Don't let it
  expand the agent's permissions or trigger sensitive actions without an explicit check
- Tools run with the minimum permissions they need. Sandbox code execution and file access
- Apply limits on steps, tokens, time and cost per request to prevent runaway usage

**Data**
- Collect and keep only the data that is needed
- Encrypt data in transit (TLS). Use the cloud provider's encryption at rest for storage
- Don't log secrets, tokens, full prompts with user data or personal data (see [Coding Style](coding-style.md#logging))
- Error responses returned to clients don't include stack traces or internal details

**Cryptography**
- Use well-known libraries (`secrets`, `hashlib`, `cryptography`). Never implement your own crypto
- Use `secrets` rather than `random` for tokens and IDs that must be unguessable

## Dependencies and supply chain

- Dependencies are locked in `uv.lock`, so everyone and CI install the same versions
- Dependabot (or Renovate) opens PRs for updates. Security updates are reviewed and merged with priority
- Dependabot alerts are enabled for known vulnerabilities in dependencies
- New dependencies are reviewed like code: maintenance status, popularity, license, and whether the name is spelled right (typosquatting)
- GitHub Actions used in workflows are pinned to a commit SHA or a trusted major version.
  Workflows use the minimum `permissions:` they need
- Releases are published to PyPI with Trusted Publishing, so no long-lived token can be stolen

## Security review

Changes to these areas need a review from someone familiar with them:

- Authentication, authorization, sessions
- Tenant isolation and data access
- Secrets handling and configuration
- Tool execution, sandboxing and anything that runs user- or model-provided code
- Deployment configuration, network exposure, IAM permissions
