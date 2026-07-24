# CI Failure Patterns Knowledge Base

This file documents known CI failure patterns and their fixes. The Code Fixer agent references this file during diagnosis.

Each pattern entry has:
- **Pattern Name**: unique identifier
- **Error Signature**: what to look for in logs
- **Root Cause**: why it happens
- **Fix**: what to change
- **Affected Files**: which files typically need changes
- **Last Seen**: date of last occurrence

---

## Pattern: missing-dependency

**Error Signature:**
```
ERROR: package not found
No package <name> available
```

**Root Cause:**
The Dockerfile references a package that does not exist in the target openEuler yum repository.

**Fix:**
1. Verify the package name is correct (check openEuler package list)
2. Try alternative package names (e.g., `python3-devel` instead of `python-devel`)
3. If the package is from EPOL, add the EPOL repo
4. Update the Dockerfile with the corrected package name

**Affected Files:** Dockerfile

**Last Seen:** -

---

## Pattern: wrong-port

**Error Signature:**
```
goss test failed: port tcp:<port> not listening
```

**Root Cause:**
The application listens on a different port than specified in goss.yaml.

**Fix:**
1. Check the Dockerfile EXPOSE directive
2. Check the application's default port from upstream documentation
3. Update goss.yaml with the correct port

**Affected Files:** goss.yaml

**Last Seen:** -

---

## Pattern: base-image-not-found

**Error Signature:**
```
manifest for openeuler/openeuler:<version> not found
```

**Root Cause:**
The specified openEuler base image version does not exist in the registry.

**Fix:**
1. Verify the openEuler version string is correct (e.g., "24.03-lts" not "24.03")
2. Check https://repo.openeuler.org/ for available versions
3. Update the ARG BASE line in Dockerfile

**Affected Files:** Dockerfile

**Last Seen:** -

---

## Pattern: meta-path-mismatch

**Error Signature:**
```
meta.yml: path does not exist
```

**Root Cause:**
The path in meta.yml does not match the actual Dockerfile location.

**Fix:**
1. Verify the directory structure matches the expected pattern
2. Update meta.yml path to match actual file location
3. Check tag format: {app-version}-{os-tag}

**Affected Files:** meta.yml

**Last Seen:** -

---

## Pattern: architecture-specific-failure

**Error Signature:**
```
Build passes on x86_64 but fails on ARM64 (or vice versa)
```

**Root Cause:**
Package availability or binary compatibility differs between architectures.

**Fix:**
1. Check if the package is available for both architectures
2. Use architecture-specific RUN instructions if needed
3. Add `arch: aarch64` or `arch: x86_64` to meta.yml if single-arch only

**Affected Files:** Dockerfile, meta.yml

**Last Seen:** -