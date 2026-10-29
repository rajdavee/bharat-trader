## What this changes

<!-- One or two sentences. Link the issue: Fixes #123 -->

## Why

## How I tested it

- [ ] `python -m unittest discover -s tests` passes locally
- [ ] New behaviour has offline tests on fixtures cut from real data
- [ ] No new dependencies (standard library only, Python 3.8+)
- [ ] No API keys, client IDs or personal data in code, fixtures or commits

## Safety (tick if your change touches the order path)

- [ ] Nothing can place an order without the user's explicit yes
- [ ] Paper mode stays the default
- [ ] trader-risk still checks the change
