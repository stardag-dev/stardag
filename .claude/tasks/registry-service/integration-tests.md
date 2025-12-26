## Status

draft

## Goal

Get automated end-to-end testing of all critical user journeys. Including the interaction between frontend (`app/stardag-ui`), backend (`app/stardag-api`), auth/keycloak and the SDK (`lib/stardag`), used as a library as well as the CLI.

Implement tests such that all relevant output/logs are captured and printed out when a test is failing. This is to facilitate AI-Agent based coding/self-verification.

## Instructions

Make a detailed plan for how to get close to complete test coverage. Identify the biggest gaps, categorize them and turn them into an implementation plan.

Some guidelines:

- Priority to add coverage to each components _independent unit tests_, but end-to-end integration tests are a necessary complement.
- For integration testing we should rely on a/the docker-compose setup primarily.
- Integration tests should be kept in a separate _python pakage_, and to the extent possible we should use _Python_ and standard pytest conventions to script the integration tests. We can for example write fixtures to bring up/down docker compose and seed the DB etc.
- We will need to to do _browser based testing_ to relly verify frontent functionality. For this we should likely use [playwright-python](https://github.com/microsoft/playwright-python) for easy of use in python scripting.
- The tests need to run in GitHub workflows (eventually), headless browser dependencies _might_ (but not necessarily) need to be dockerized as well.

## Context

Background information, related files, prior discussions.

## Execution Plan

### Summary Of Preparatory Analysis

### Plan

1. Step one
2. Step two
3. ...

## Decisions

Key decisions made and their rationale.

## Progress

- [x] Completed item
- [ ] Pending item

## Notes

Any additional observations, blockers, or open questions.
