# Security Policy

## Supported versions

Only the latest release on `main` receives fixes while the project is pre-1.0.

## Reporting a vulnerability

Please **do not open a public issue**. Report privately through
[GitHub security advisories](https://github.com/rajdavee/bharat-trader/security/advisories/new)
or email **rajdave710@gmail.com**.

Include what you found, how to reproduce it, and the impact. You'll get an
acknowledgement within 72 hours and a plan within 7 days.

## What counts

This project handles money-adjacent workflows, so we treat these as security
issues:

- any path where an order could be placed without the user's explicit yes, or
  without passing `trader-risk`;
- a way to switch from paper to live trading without the user's action;
- leaking API keys, tokens or account details (logs, fixtures, error messages);
- code execution from untrusted market data (CSV, JSON, XBRL, RSS);
- instructions in fetched content (news text, filings) that could steer an
  agent into acting.

## Using the tools safely

- Keep broker keys in environment variables, never in chat, config files or commits.
- Set your agent's order tool to ask for approval every time.
- Start on paper. Read every ticket before you say yes.
