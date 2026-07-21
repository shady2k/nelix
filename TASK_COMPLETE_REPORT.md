# Task Complete: Self-Fetching Bootstrapper (nelix-657)

## Summary

Both tasks from `docs/superpowers/plans/2026-07-21-nelix-fetching-bootstrapper.md` implemented
by TDD — the release now bakes the manifest digest into the `.pyz` (and excludes the `.pyz`
from the manifest to break the circularity), and `install` with no arguments fetches the
pinned release from the baked-in `BASE_URL`, verifies every artifact, and only then builds.

## Commits

1. `177c69a` — release: bake the manifest pin into the pyz, and take the pyz out of the manifest
2. `a4886e2` — bootstrap: install with no arguments — fetch the pinned release, verify it, build

## Test Suite

```
2213 passed, 1 skipped in 35.77s
```

## End-to-End Proof

### Build release + server
```
$ python release.py --version "0.1.0"
$ python3 -m http.server 19876 --directory dist_e2e3 &
```

### Install with NO bundle args (uses baked-in pin)
```
$ python3 dist_e2e3/nelix-bootstrap.pyz install --home /tmp/nelix-e2e-test-home3
{"build": "0.1.0-1d59f9d4be2a", "home": "/private/tmp/nelix-e2e-test-home3",
 "launcher": "/private/tmp/nelix-e2e-test-home3/bin/nelix", "version": "0.1.0",
 "bootstrap_schema": 1, "ok": true}
```

### Installed launcher works
```
$ python3 /tmp/nelix-e2e-test-home3/bin/nelix --help
usage: nelix [-h] {daemon,launcher,rpc,wait,config} ...
```

### git diff --stat (both commits)
```
 bootstrap/cli.py              |   4 +-
 bootstrap/install.py          | 107 ++++++++++++++++++++++++++++++++--
 release.py                    |  37 ++++++++++++---
 tests/test_bootstrap_fetch.py |  80 +++++++++++++++++++++++
 tests/test_bootstrap_pyz.py   |  39 +++++++++++++--
 5 files changed, 249 insertions(+), 18 deletions(-)
```
