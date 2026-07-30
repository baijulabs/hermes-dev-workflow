# Case Study: Vue Milestone Completion Conflict (PR #766 vs PR #768)

Real-world session case study on resolving a structural merge conflict in a Vue SFC template/script block where two parallel PRs modified the exact same section of code for different, non-conflicting business goals.

## Context
- **PR #766 (Commit `65ecab7b`)**: Introduced backend milestone completion tracking (`completeMilestone('ob_culture_mapped')`) on Step 3 completion to ensure metrics are recorded even if the backend event-listener fails.
- **PR #768 (Commit `aeaa34ca`)**: Implemented user-facing UI updates, setting `workspaceStatus.value = 'completed'` and pushing a localized success message to the chat message stream upon Step 3 completion.
- Both modified the `organizational_blueprint` conditional block in the `handleCompletionCta()` function of `frontend/src/views/Chat.vue`.

---

## The Conflict Markers
During a local rebase of `wt/t_d2704d8d` (PR #768) onto `origin/main` (which already had PR #766), Git stopped with the following conflict in `frontend/src/views/Chat.vue`:

```vue
    if (completionTopic.value === 'organizational_blueprint') {
      const result = await completeStep3();
<<<<<<< HEAD
      await completeMilestone(topic.value, 'ob_culture_mapped').catch(() => {});
=======
      workspaceStatus.value = 'completed';
      messages.value.push({ sender: 'ai', text: t('step3.completeSuccess', 'Step 3 (Organizational Blueprint) has been completed successfully!') });
>>>>>>> aeaa34ca (fix: milestone refresh after step 3 completion via CTA banner)
      if (result?.milestone_ts || (Array.isArray(result?.milestone_updates) && result.milestone_updates.length > 0)) {
```

---

## Resolution Strategy: Combined Intent
This is a classic **Structural Conflict** where both sides are correct and necessary. The resolution is to accept both intents and sequence them logically:

1. **Keep the Backend tracking**: First, call `await completeMilestone(...)` to ensure the milestone is successfully recorded.
2. **Apply the UI updates**: Next, set `workspaceStatus.value = 'completed'` and push the localized success message to the chat stream.
3. **Verify Unconditional Refresh**: Ensure the milestone refresh triggers regardless of the backend response shape.

### Merged Code Resolution
```vue
    if (completionTopic.value === 'organizational_blueprint') {
      const result = await completeStep3();
      await completeMilestone(topic.value, 'ob_culture_mapped').catch(() => {});
      workspaceStatus.value = 'completed';
      messages.value.push({ sender: 'ai', text: t('step3.completeSuccess', 'Step 3 (Organizational Blueprint) has been completed successfully!') });
      if (result?.milestone_ts || (Array.isArray(result?.milestone_updates) && result.milestone_updates.length > 0)) {
        triggerMilestoneRefresh();
      } else {
        triggerMilestoneRefresh();
      }
    }
```

---

## Key Lessons
1. **Don't Pick Sides on Structural Conflicts**: When conflicts occur in a shared function block, analyze the commit history of `main` (the `HEAD` side) and your branch (the incoming side) to understand the *business intent* of both. Almost always, the solution is a sequential merge of both intents rather than selecting one over the other.
2. **Isolate with Worktrees**: Always perform the rebase and conflict resolution in a dedicated, isolated git worktree (e.g. `.worktrees/rebase-<id>`) to avoid disrupting your main repository branch.
3. **Leverage Non-Interactive Rebasing**: In automated CLI environments where git rebase prompts for an interactive commit message editor, bypass the prompt by prepending `GIT_EDITOR=true`:
   ```bash
   git add frontend/src/views/Chat.vue
   GIT_EDITOR=true git rebase --continue
   ```
