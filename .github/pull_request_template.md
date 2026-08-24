## Outcome

Describe the user-visible problem and the result of this change.

## Security impact

Describe any change to project authorization, filesystem access, subprocess execution, network access, session visibility, confirmation tokens, limits, or sensitive-data handling.

## Verification

- [ ] `./scripts/check_public_release.py`
- [ ] `.venv/bin/python -m unittest discover -s tests -v`
- [ ] Python sources compile
- [ ] Shell scripts pass `sh -n`
- [ ] Documentation and configuration examples match the implementation
- [ ] No real keys, tunnel IDs, private paths, project content, logs, or session data are included

## Compatibility

List the tested operating system, Python version, Codex CLI version, and any expected migration or rollback behavior.
