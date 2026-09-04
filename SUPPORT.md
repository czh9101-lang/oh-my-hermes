# Support

Use GitHub issues for bugs, feature requests, and documentation gaps.

Before opening an issue:

```sh
omh doctor
omh list
omh apply --dry-run
```

Include command output, OS, Python version, Hermes config location, and whether
the issue affects install, routing, generated skill text, or config
registration.

## Chat-native issue intake

In a Hermes chat surface you can also ask for filing directly, for example
"please file this as an issue: omh setup fails on Windows" (Korean works too:
"이 버그를 깃허브 이슈로 올려줘"). The `github-issue-intake` workflow then:

1. Classifies the report and asks at most three decision-changing questions
   (desired outcome, scope boundary, missing evidence).
2. Searches for duplicates and presents a direction check that separates
   observed evidence from inference.
3. Waits for your explicit confirmation. The only write a public reporter can
   authorize is one idempotency-keyed issue creation against an explicit
   repository. A maintainer file-now transition requires an authenticated
   wrapper observation with actor and evidence identity; it never skips form,
   duplicate, security, or read-back gates.
4. Builds the request only from the checked-in issue form; arbitrary title,
   body, or labels remain prepared-only. It hands that complete transient request
   to an authorized Hermes-native/wrapper connector. Core OMH never calls GitHub
   itself; the connector must enforce the stable idempotency key, and core marks
   dispatch as consumed so dispatched or observed artifacts cannot hand off
   again. The persisted `github_issue_intake/v1` record contains bounded
   metadata, digests, and refs only. No issue is claimed as filed until connector
   read-back (repository, author, title, body, labels, URL, request key) is
   verified.

Security vulnerabilities are never filed as public issues through this path;
use the private reporting process in `SECURITY.md` instead.

