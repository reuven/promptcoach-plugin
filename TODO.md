# TODO

## After v0.1.0

### Coach commands fail closed

- [x] A coach command the server doesn't answer (timeout, connection error,
      5xx, malformed or non-reply JSON, refused redirect) is blocked locally
      with "I didn't answer in time ... send it again", never sent to Claude.
- [x] A well-formed non-blocking reply to a command is blocked too (ruling,
      2026-11-18): with the server's message as the reason when it sends
      one (sign-in, lapsed), else the "didn't answer" text. A command typed
      with no token is blocked with a setup reminder.
- [x] Hypothesis (dev only): a never-crash property for `before()` and a
      no-echo property for the command block; dev/ci/mutmut profiles.

### Redaction (`redact`)

- [ ] Multi-line `<password>` elements (`<password>\n  value\n</password>`).
- [ ] Value-before-key attributes (`<add value="..." key="DbPassword"/>`).
- [ ] Positional credentials in calls other than `login(...)`:
      `auth("user", "pass")`, `HTTPBasicAuth("user", "pass")`.
- [ ] `is` / `in` comparisons (`password is "..."`, `password in ("...", ...)`).
- [ ] Letter-glued generic tokens (`xsk-...`, `myhf_...`, `xeyJ...`): an
      accepted tradeoff, since matching them would also hit words such as
      `task-management-...`.
- [ ] `gh secret set NAME --body VALUE`.
- [ ] FTP `PASS value`.
- [ ] "Password for user ...:" prompts echoed with a value after them.

### Also seen in review, not yet scheduled

- [ ] Comparison with a call holding a literal (`token == bytes.fromhex(deadbeef)`)
      and a walrus with a literal call argument (`k := decode("...")`) are kept.
- [ ] A JWT straddling the reply's cut can keep its payload and signature.
- [ ] Commands not flagged by `prints_secrets`: `npm config get ..._authToken`,
      `docker-credential-* list`, `compgen -e`, `cat ~/.config/hub`,
      `fly secrets list`, `git config --get credential.helper`.
