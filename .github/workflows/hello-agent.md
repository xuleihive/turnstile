---
on:
  workflow_dispatch:

permissions:
  contents: read

network: defaults
---

# Hello Agent

You are performing a read-only validation of GitHub Agentic Workflows in this repository.

## Instructions

1. Read the repository's README and inspect the top-level directory structure.
2. Identify the apparent purpose of the repository.
3. Summarize the main technology stack based only on files found in the repository.
4. Report whether the repository appears to contain automated tests.
5. List the three most relevant files or directories you inspected.

## Safety requirements

- Do not modify, create, rename, or delete any repository files.
- Do not create issues, pull requests, comments, branches, commits, or releases.
- Do not execute deployment commands.
- Do not access external systems or production environments.
- If information is uncertain, explicitly state that it is uncertain.

Return a concise Markdown report.
