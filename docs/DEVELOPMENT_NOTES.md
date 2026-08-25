# Development Contracts

This portfolio snapshot preserves the source project's core engineering invariants.

## 1. Product scope

The application is a local, single-user UFC DFS research tool. Classic is the primary persisted workflow. Captain support is additive and isolated. There is no hosted backend, authentication system, payment flow, DraftKings account integration, or automatic contest entry.

## 2. Data acquisition

Supported inputs are explicit and user-triggered: salary CSV upload; odds CSV, paste/table, snapshot, manual entry; and an approved public static-HTML fetch path. Page load must not trigger remote fetching or database writes. Authenticated, paywalled, CAPTCHA-bypassing, proxy-bypassing, premium, or background scraping is out of scope.

## 3. Projection contract

The default projection is:

```text
implied_win_probability * 70 + value_gap_bonus + five_round_bonus
```

The coefficients and thresholds are pinned by tests. Additional methods are additive and must not silently replace the default.

## 4. Persistence and write safety

SQLite writes go through repositories/services and explicit UI actions. Page rendering and preview operations should be read-only. Schema changes require paired migrations and tests. Review overrides are scoped to their slate and must preserve transaction boundaries.

## 5. Data hygiene

Never commit salary or odds exports, SQLite databases, uploaded manifests, generated per-slate data, lineup exports, environment files, credentials, or operator logs. Synthetic fixtures are allowed and must be clearly identified.

## 6. Validation language

The included automated suite exercises pure logic, persistence, and Streamlit surfaces. Real-file compatibility remains a separate validation step. Do not describe an importer or experimental workflow as validated against a current official feed unless that run has actually been performed.
