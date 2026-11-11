## Summary

<!-- What does this change do, and why? -->

## Cross-repo completeness

This repo is part of a multi-repo system (the hub is `snaphouse`). A feature
usually spans repos, so confirm the full surface of this change, or mark a line
N/A with a reason. See the hub repo's `docs/cross-repo-features.md` for the map
and the definition of done.

- [ ] Backend logic / the API endpoint or field (request + response models)
- [ ] The frontend API client fn and its types (both)
- [ ] The frontend UI control (route or component) + user docs
- [ ] Any other service's endpoint/index this touches (the snaphouse role, units, nginx)
- [ ] Config / env / deploy (install, units, local config) in the hub repo
- [ ] Docs updated in EACH affected repo (README / AGENTS.md / guide)
- [ ] Any deferred slice explicitly noted and tracked
