# TODO

## After v0.1.0

### Coach commands fail closed

- [x] A coach command the server doesn't answer (timeout, connection error,
      5xx, malformed or non-reply JSON, refused redirect) is blocked locally
      with "I didn't answer in time ... send it again", never sent to Claude.
- [ ] Decide: a well-formed *non-blocking* reply to a command (the server's
      quiet pool-timeout and rate-limit replies, and its not-signed-in and
      lapsed messages) still passes the command to Claude. The server's
      contract allows these replies for a command, so blocking them needs a
      ruling on the reason text (the sign-in and lapsed messages must still
      be shown).
- [ ] Hypothesis is not a dev dependency, so the fail-closed rule has
      example tests only; a property ("a command never gets a non-block
      reply; an ordinary prompt never gets a local block") needs it added.

Deferred from the v0.1.0 review rounds. The server scrubs everything again, so
each of these is a gap in the client's first filter, not a leak past the coach.

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
