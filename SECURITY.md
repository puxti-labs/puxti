# Security Policy

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report security issues by emailing **puxti@okolico.com** with:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof of concept
- The puxti version affected (`puxti --version`)

You can expect an acknowledgement within 2 business days and a resolution timeline within 7 days of confirmation.

## Scope

In scope:

- Prompt injection via dbt model SQL or user-supplied input
- Credential exposure (API keys, tokens)
- Dependency vulnerabilities with a credible attack path against puxti users

Out of scope:

- Vulnerabilities in self-hosted infrastructure (Neo4j, GitHub Actions runners) that are the user's responsibility to secure
- Issues requiring physical access to a user's machine

## Supported Versions

Only the latest published version receives security fixes.
