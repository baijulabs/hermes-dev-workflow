# Multi-Agent AGENTS.md Pattern

When running a kanban fleet with multiple profiles (orchestrator → coder → code-reviewer), a single monolithic AGENTS.md with `alwaysApply: true` injects ALL instructions into EVERY profile. Sections meant for the orchestrator (planning, PR creation, versioning) leak into coder and reviewer contexts, wasting tokens and potentially causing wrong behaviour.

## The Pattern: Profile-Tiered Sections

Restructure AGENTS.md into clearly scoped tiers with a Profile Guidance table at the top.

### Structure

```
AGENTS.md
─── Profile Guidance (instructions table for each role)
─── Tier 1: Project Reference (all profiles)
│   ├── Architecture overview
│   ├── Testing standards & conventions
│   ├── Project-specific patterns & gotchas
│   └── Setup instructions
─── Tier 2: Coder Instructions (coder profile)
│   ├── Single-card execution protocol
│   ├── Handoff format (kanban_complete metadata)
│   ├── Coding standards
│   └── What NOT to do
─── Tier 3: Reviewer Instructions (code-reviewer profile)
│   ├── FAIL-CLOSED rules
│   ├── Blocking vs non-blocking item lists
│   └── Handoff format (kanban_block on failure)
─── Tier 4: Orchestrator Instructions (orchestrator profile)
│   ├── Planning & PRD generation
│   ├── Task decomposition
│   ├── Pre-merge QA gates
│   ├── PR creation & merge management
│   └── Versioning & release workflow
─── Appendix A: Git Workflow (all profiles)
─── Appendix B: GCP/CI/CD (orchestrator/CI only)
─── Appendix C: Build/Deploy (orchestrator/CI only)
```

### Profile Guidance Table

At the top of AGENTS.md, add a table telling each profile which sections to read and which to ignore:

```markdown
| Profile | Read these sections | Ignore |
|---|---|---|
| **coder** | Tier 1, Tier 2, Appendix A | Tier 3, Tier 4, Appendix B, C |
| **code-reviewer** | Tier 1, Tier 3 | Tier 2, Tier 4, Appendix B, C |
| **orchestrator** | Tier 1, Tier 4, Appendix B, C | Tier 2, Tier 3 |
```

This is a human-readable convention — the Hermes framework does not programmatically filter by profile. Each agent reads the table and follows the instructions for its own role.

### Key Principles

1. **`alwaysApply: true` stays** — the entire file is injected into every profile's system prompt. The tier structure + guidance table makes the content safe for all readers.

2. **Tier 1 is the safety net** — architecture, testing standards, and project patterns are relevant to every agent type.

3. **Explicit "what NOT to do" sections** — each profile-tier should have an explicit list of actions the agent must NOT take:
   - Coder: "Do not open a PR. Do not merge to main. Do not run feature-plan/feature-prep/feature-build/feature-close."
   - Reviewer: "Do not implement fixes. Do not make changes to code."

4. **Push CI/CD and deploy content to appendices** — most workers never need GCP WIF setup, Dockerfile paths, or production deployment details. Move these out of the main flow.

5. **Testing standards belong in Tier 1** — all profiles need to understand how tests work, even if only coders run them. This includes coverage targets, test data patterns, and mocking conventions.

## Backwards-Compatible Migration

1. Create a backup: `cp AGENTS.md AGENTS.md.bak`
2. Add the Profile Guidance table at the top (after frontmatter)
3. Move orchestrator-only sections (planning, PRD, versioning, release, feature-close) into Tier 4
4. Move coder-specific execution rules into Tier 2
5. Move reviewer rules into Tier 3
6. Push GCP/deploy content to appendices
7. Keep architecture, testing, and patterns in Tier 1

## Real-World Example

The MyProject AGENTS.md was restructured from a 289-line monolithic file (designed for a single agent owning the full lifecycle) into a 335-line tiered structure. The file grew by ~15% but each profile now sees a focused document. Key changes:

- Coder: was told "work through all sub-tasks... commit after each... continue to next... open a PR" — now told "implement ONE card, hand off, stop"
- Reviewer: had no role-specific content — now has FAIL-CLOSED rules and structured handoff format
- Orchestrator: had planning mixed with execution — now owns the full lifecycle separately
- Appendices: GCP WIF (53 lines) and Build/Deploy (3 lines) moved out of main flow

## Limitations

- **No programmatic enforcement.** The Profile Guidance table is human-readable text. An agent can still read sections it should ignore. In practice, the explicit "what NOT to do" instructions at the top of each tier are effective at preventing wrong behaviour.
- **Token budget.** All sections are still injected into every profile. If token budget is critical (small-context models), consider splitting into per-profile files instead.