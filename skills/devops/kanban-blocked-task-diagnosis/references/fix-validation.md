# Fix Validation: Confirm Root Cause Before Making Changes

## The Core Principle

**Never change a configuration, workflow file, or setup unless you've confirmed the root cause and verified the existing config is actually broken.**

Applying this principle prevents:
- Unnecessary noise in diffs that erode reviewer trust
- Introducing new failure modes from needless changes
- Masking the real root cause behind a superfluous edit

## The Technique: Test the Fix in Isolation

When you have a hypothesis about a fix, follow this sequence:

1. **Identify the real fix** — the one thing that actually addresses the root cause (e.g., regenerate the lock file)
2. **Apply only that fix** — nothing else
3. **Test the fix** with the existing configuration unchanged — does it pass?
4. **Only then assess** whether the configuration/workflow is also broken
5. **If the real fix works without the config change** — the config change was unnecessary. Skip it.
6. **If the real fix fails** — the root cause hypothesis was wrong. Return to diagnosis, don't change configs.

## Example: The Stale Lock File Fix

**Hypothesis:** `npm ci` fails because `package-lock.json` is stale.

**Correct approach:**
1. Run `npm install` to regenerate the lock file (the real fix)
2. Test `npm ci` with the existing workflow unchanged — does it pass?
3. If yes, the fix is complete. No workflow changes needed.
4. If no, investigate further — the root cause may be different.

**Wrong approach (what I did):**
1. Assume the `working-directory` in the workflow is also wrong
2. Change the workflow AND regenerate the lock file simultaneously
3. Test — now you can't tell which change fixed it or if the workflow change was needed

## Red Flags — You're About to Make an Unnecessary Change

- "This config looks wrong to me" — without verifying it actually causes the failure
- "I'll fix two things at once" — you can't isolate what worked
- "The working-directory should be X" — without testing whether the current one works with the real fix
- "While I'm here, let me also fix this" — scope creep on a fix

## Verification Checklist

- [ ] Single root cause hypothesis formed
- [ ] One change applied (the hypothesised fix)
- [ ] Tested with original configuration intact
- [ ] Confirmed fix works without config changes
- [ ] Only then assessed whether config changes are also needed